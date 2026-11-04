"""Sequential native cases, durable accounting, and conservative recovery gates."""

import argparse
import csv
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from runtime.guardian import Guardian, INTERVAL, STOP_BUDGET
from runtime.health import cluster_errors, kubectl, recovery_errors, sample_all
from runtime.remote import ROOT
from runtime.sources import check_canonical, git, verify_runtime

RUNS = ROOT / "private/runs"
INFRA = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "chaos-mesh",
    "openebs",
    "sregym",
}


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def journal(run, kind, **fields):
    value = {"kind": kind, "time": datetime.now(timezone.utc).isoformat(), **fields}
    with (run / "journal.jsonl").open("a") as handle:
        handle.write(json.dumps(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(value), flush=True)
    return value


def records(run):
    path = run / "journal.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def closed_cases(entries):
    """Recovery receipts update operations only; never scores/interventions."""
    closed = {}
    for entry in entries:
        problem = entry.get("problem")
        if entry["kind"] == "case_end":
            closed[problem] = dict(entry)
        elif entry["kind"] == "post_case_recovery" and problem in closed:
            for field in ("cleanup_verified", "recovered"):
                closed[problem].setdefault("initial_" + field, closed[problem][field])
                closed[problem][field] = entry[field]
    return closed


def residue():
    errors = []
    objects = json.loads(
        kubectl(
            "get",
            "pods,deployments,statefulsets,daemonsets,jobs,cronjobs,replicasets",
            "-A",
            "-o",
            "json",
        )
    )["items"]
    for obj in objects:
        meta = obj["metadata"]
        if meta["namespace"] not in INFRA:
            errors.append(f"Remaining {obj['kind']} {meta['namespace']}/{meta['name']}")
    names = subprocess.check_output(
        ["docker", "ps", "--format", "{{.Names}}"], text=True, timeout=10
    ).splitlines()
    errors += [
        f"Remaining agent/tool container {name}"
        for name in names
        if name.startswith("sregym-")
    ]
    return errors


def recovered(config, run, phase, budget=180):
    deadline, passes = time.monotonic() + budget, 0
    while True:
        errors = cluster_errors(config)
        try:
            errors += residue()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"Residue observation failed: {type(exc).__name__}")
        samples = sample_all(config)
        errors += recovery_errors(samples)
        save(
            run / "health.json",
            {"phase": phase, "time": time.time(), "samples": samples, "errors": errors},
        )
        passes = passes + 1 if not errors else 0
        if passes >= 2:
            return True
        if time.monotonic() >= deadline:
            journal(run, "recovery_failed", phase=phase, errors=errors)
            return False
        time.sleep(5)


def native_evidence(case, problem):
    base = case / "results/holmesgpt" / problem
    rows, phases, errors = [], [], []
    path = base / f"{problem}_holmesgpt_results.csv"
    if path.exists():
        with path.open() as handle:
            rows = list(csv.DictReader(handle))
    ledger = base / "phases_attempt1.jsonl"
    if ledger.exists():
        phases = [json.loads(line) for line in ledger.read_text().splitlines()]
    cleanup = [p for p in phases if p.get("phase") == "cleanup"]
    if len(rows) != 1:
        errors.append("Exactly one native final result row is required")
    if (
        len(cleanup) != 2
        or cleanup[0].get("event") != "start"
        or cleanup[1].get("event") != "end"
        or cleanup[1].get("outcome") != "ok"
    ):
        errors.append("Paired successful native cleanup phase is missing")
    row = rows[0] if len(rows) == 1 else {}
    if row and (row.get("problem_id") != problem or row.get("attempt") != "1"):
        errors.append("Native result identity differs from this attempt")
    for flag in ("cleanup_failed", "cleanup_timed_out", "artifact_finalization_failed"):
        if row.get(flag, "").lower() not in ("", "false", "0"):
            errors.append(f"Native {flag}: {row.get(flag)}")
    return row, errors


def group_alive(pgid):
    # Ignore reaped-workload zombies, but do not silently kill residual helpers.
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop_owned_helpers(pgid):
    """After native grading/cleanup, reap only this case's remaining group."""
    if not group_alive(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 5
        while group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.1)
    return not group_alive(pgid)


def case_command(config, problem):
    return [
        str(ROOT / ".work/host/bin/python"),
        "-u",
        "main.py",
        "--problem",
        problem,
        "--agent",
        "holmesgpt",
        "--model",
        config["model"],
        "--judge-model",
        config["judge_model"],
        "--profile",
        "full",
        "--n-attempts",
        "1",
        "--agent-timeout",
        "1800",
    ]


def execute(config, run, problem, stop_requested):
    case = run / "cases" / problem
    case.mkdir(parents=True)  # Refuse accidental replay over an earlier attempt.
    journal(run, "case_start", problem=problem)
    env = os.environ.copy()
    env.update(
        ANTHROPIC_API_KEY=Path(config["api_key_file"]).expanduser().read_text().strip(),
        SREGYM_AGENT_IMAGE=config["image"],
        SREGYM_WORKER_SSH_KEY=config["worker_key"],
        SREGYM_RESULT_DIR=str(case / "results"),
        SREGYM_CACHE_DIR=str(run / "cache"),
        PYTHONUNBUFFERED="1",
    )
    native = ROOT / ".work/checkouts/SREGym"
    env["PYTHONPATH"] = str(
        native
    )  # Host never imports the agent-side SDK environment.
    monitor, intervention, stopping, forced = Guardian(config), None, None, False
    with (case / "console.log").open("x") as output:
        proc = subprocess.Popen(
            case_command(config, problem),
            cwd=native,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            save(
                run / "active.json",
                {
                    "problem": problem,
                    "pid": proc.pid,
                    "case": str(case),
                    "time": time.time(),
                },
            )
            while proc.poll() is None:
                observation = monitor.observe()
                save(run / "observation.json", observation)
                reasons = observation["reasons"] + (
                    ["Operator requested stop"] if stop_requested[0] else []
                )
                if reasons and intervention is None:
                    # Durable BEFORE sending any signal. Late grades never undo this latch.
                    intervention = journal(
                        run,
                        "intervention",
                        problem=problem,
                        reasons=reasons,
                        observation=observation,
                    )
                    stopping = time.monotonic()
                    if proc.poll() is None:
                        proc.send_signal(signal.SIGINT)
                if stopping is not None and time.monotonic() - stopping > STOP_BUDGET:
                    journal(
                        run,
                        "forced_stop",
                        problem=problem,
                        reason="Native interrupt/cleanup deadline expired",
                    )
                    os.killpg(proc.pid, signal.SIGKILL)
                    forced = True
                    break
                time.sleep(INTERVAL)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                # Failed accounting/monitoring is an unresolved attempt. Stop the
                # native runner, never publish a closed/graded supervisor record.
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=STOP_BUDGET)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
    row, errors = native_evidence(case, problem)
    if forced:
        errors.append("Forced termination: fault recovery is unknown")
    if not errors and group_alive(proc.pid):
        journal(run, "post_evaluation_helpers", problem=problem, process_group=proc.pid)
        if not stop_owned_helpers(proc.pid):
            errors.append("Native process group still has live traffic/helpers")
    elif group_alive(proc.pid):
        errors.append("Native process group still has live traffic/helpers")
    safe = not errors and recovered(config, run, f"post:{problem}")
    graded = row.get("run_status") == "complete" and intervention is None
    result = {
        "problem": problem,
        "returncode": code,
        "process_group": proc.pid,
        "graded": graded,
        "intervened": intervention is not None,
        "cleanup_verified": not errors,
        "recovered": safe,
        "errors": errors,
        "native_result": row,
        "status": "graded" if graded else "unscored",
    }
    journal(run, "case_end", **result)
    (run / "active.json").unlink(missing_ok=True)
    return safe and not stop_requested[0]


def main(run_name):
    os.umask(0o077)
    config = json.loads((ROOT / "private/deployment.json").read_text())
    os.environ["PATH"] = (
        str(ROOT / ".work/bin")
        + ":"
        + str(ROOT / ".work/host/bin")
        + ":"
        + os.environ["PATH"]
    )
    check_canonical()
    if git(ROOT, "status", "--porcelain"):
        raise RuntimeError("Commit/preserve source edits before launching a campaign")
    for name in ("SREGym", "holmesgpt"):
        verify_runtime(name, ROOT / ".work/checkouts" / name)
    run = RUNS / valid_run(run_name)
    selection = json.loads((run / "campaign.json").read_text())
    # An advisory lock, not file existence, decides ownership. Never inherited.
    with (ROOT / "private/campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entries = records(run)
        started = {r["problem"] for r in entries if r["kind"] == "case_start"}
        closed = closed_cases(entries)
        if started - closed.keys():
            raise RuntimeError(
                "An unclosed native attempt needs manual recovery; refusing automatic replay"
            )
        if any(not c["recovered"] for c in closed.values()):
            raise RuntimeError(
                "A prior case has unresolved recovery; inspect it before a new campaign"
            )
        stopped = [False]
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
        journal(run, "supervisor_start", pid=os.getpid(), revision=git(ROOT, "rev-parse", "HEAD"))
        if selection["deployment"] != config:
            raise RuntimeError(
                "Deployment/configuration changed since campaign creation"
            )
        if not recovered(config, run, "preflight"):
            journal(run, "halt", reason="Preflight recovery gate failed")
            return
        for problem in selection["problems"]:
            if problem in closed:
                continue
            if stopped[0] or not execute(config, run, problem, stopped):
                journal(
                    run,
                    "halt",
                    reason="Stop requested or recovery could not be verified",
                )
                return
        journal(run, "finished", cases=len(selection["problems"]))


def valid_run(name):
    if not re.fullmatch(r"run-[0-9]{2,}", name):
        raise ValueError("Run name must be run-NN")
    return name


def create(problem=None):
    config = json.loads((ROOT / "private/deployment.json").read_text())
    native = ROOT / ".work/checkouts/SREGym"
    ids = json.loads(
        subprocess.check_output(
            [
                str(ROOT / ".work/host/bin/python"),
                "-c",
                "import json; from sregym.conductor.problems.registry import ProblemRegistry; print(json.dumps(ProblemRegistry().get_problem_ids()))",
            ],
            cwd=native,
            text=True,
        )
    )
    if problem and problem not in ids:
        raise ValueError(f"Unknown problem {problem}")
    RUNS.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        run = RUNS / f"run-{number:02d}"
        try:
            run.mkdir()
            break
        except FileExistsError:
            number += 1
    image = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", config["image"]], text=True
        )
    )[0]
    image_revision = image["Config"]["Labels"]["org.opencontainers.image.revision"]
    if git(
        ROOT,
        "diff",
        image_revision,
        "HEAD",
        "--",
        "runtime/agent.py",
        "runtime/recording.py",
        "images/agent.lock",
        "images/build.lock",
        "images/Dockerfile",
        "patches/holmesgpt",
    ):
        raise RuntimeError(
            "Agent image inputs changed; rerun deployment to build the recorded variant"
        )
    save(
        run / "campaign.json",
        {
            "run": run.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "problems": [problem] if problem else ids,
            "deployment": config,
            "supervisor_revision": git(ROOT, "rev-parse", "HEAD"),
            "image_id": image["Id"],
            "image_revision": image_revision,
            "settings": {
                "profile": "full",
                "agent": "holmesgpt",
                "max_steps_per_stage": 100,
                "shared_agent_timeout_seconds": 1800,
                "attempts_per_problem": 1,
                "internet_access": "filtered",
                "container_hardening": "on",
            },
        },
    )
    print(run.name)
    return run.name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--problem")
    parser.add_argument("--run")
    args = parser.parse_args()
    if args.create:
        create(args.problem)
    elif args.run:
        main(args.run)
    else:
        parser.error("Choose --create or --run")
