#!/usr/bin/env python3
"""Collect an idle campaign privately; optionally share eligible normalized trajectories."""

import argparse
import csv
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from runtime.campaign import closed_cases, valid_run
from runtime.convert import convert
from runtime.remote import ROOT, load_config, ssh_command

# An explicit benchmark-only allowlist: no caches, volumes, system logs, keys,
# worker data, source trees, guardian samples, or broad recovery directories.
PACKAGE = r"""
import fcntl,io,json,os,sys,tarfile
from pathlib import Path
root=Path('private/runs')/sys.argv[1]
lock=Path('private/campaign.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
active=root/'active.json'
if active.exists():
 pid=json.loads(active.read_text())['pid']
 try:os.kill(pid,0)
 except ProcessLookupError:pass
 else:raise RuntimeError('Native case may still be active; collect after it stops')
paths=[root/'campaign.json']
if (root/'journal.jsonl').exists():paths.append(root/'journal.jsonl')
for case in sorted((root/'cases').glob('*')):
 paths += list(case.glob('console.log'))
 paths += list(case.glob('results/*_ALL_results.csv'))
 paths += list(case.glob('results/holmesgpt/*/*.csv'))
 paths += list(case.glob('results/holmesgpt/*/phases*.jsonl'))
 # Published or unpublished native capture; both remain private.
 for result in case.glob('results/**/overlay/records.jsonl'):
  paths.append(result)
  for stage in (result.parent.parent/'native').glob('*'):
   paths += list(stage.glob('events.jsonl'))+list(stage.glob('result.json'))
count=size=0
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
 for path in sorted(set(paths)):
  if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):raise RuntimeError('Refusing nonregular/symlink evidence')
  archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
  count+=1;size+=path.stat().st_size
print('COLLECT '+json.dumps({'files':count,'bytes':size}),file=sys.stderr)
"""


def receive(process, destination):
    count = size = 0
    with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
        seen = set()
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or member.name in seen
            ):
                raise ValueError(f"Unsafe/duplicate archive entry: {member.name}")
            seen.add(member.name)
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            if path.stat().st_size != member.size:
                raise ValueError("Truncated transfer")
            count += 1
            size += member.size
    error = process.stderr.read().decode(errors="replace")
    if process.wait():
        raise RuntimeError(f"Remote collection failed: {error.strip()}")
    summary = json.loads(
        next(line[8:] for line in error.splitlines() if line.startswith("COLLECT "))
    )
    if summary != {"files": count, "bytes": size}:
        raise ValueError("Transfer counts differ")
    return summary


def merge(source, target):
    # Validate all existing files before copying any additions.
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        old = target / relative
        if old.exists() and old.read_bytes() != path.read_bytes():
            if str(relative) != "journal.jsonl" or not path.read_bytes().startswith(
                old.read_bytes()
            ):
                raise ValueError(f"Collection conflicts with preserved file: {old}")
    for path in source.rglob("*"):
        if path.is_file():
            old = target / path.relative_to(source)
            old.parent.mkdir(parents=True, exist_ok=True)
            if not old.exists() or old.read_bytes() != path.read_bytes():
                shutil.copyfile(path, old)


def write_same(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Export differs from existing file: {path}")
    else:
        path.write_bytes(data)


def outputs(bundle, run_name):
    campaign = json.loads((bundle / "campaign.json").read_text())
    if campaign["run"] != run_name:
        raise ValueError("Campaign identity differs from selected run")
    journal = bundle / "journal.jsonl"
    events = (
        [json.loads(l) for l in journal.read_text().splitlines()]
        if journal.exists()
        else []
    )
    starts = {e["problem"] for e in events if e["kind"] == "case_start"}
    ends = closed_cases(events)
    rows, eligible = [], {}
    captured = 0
    for problem in campaign["problems"]:
        if problem not in starts:
            continue
        end = ends.get(problem, {})
        case = bundle / "cases" / problem
        if (
            problem in ends
            and not (
                case / f"results/holmesgpt/{problem}/{problem}_holmesgpt_results.csv"
            ).is_file()
        ):
            raise ValueError(f"Missing original native result CSV for {problem}")
        evidence = sorted(case.glob("results/**/overlay/records.jsonl"))
        trajectory = convert(evidence[0].parent.parent) if len(evidence) == 1 else None
        complete = bool(trajectory and trajectory["extra"]["capture_complete"])
        session = trajectory["session_id"] if trajectory else None
        if trajectory:
            trajectory["extra"]["sregym"] = {
                "run": run_name,
                "problem_id": problem,
                "session_id": session,
                "graded": end.get("graded", False),
                "guardian_intervened": end.get("intervened"),
                "native_result_path": f"cases/{problem}/results/holmesgpt/{problem}/{problem}_holmesgpt_results.csv",
            }
            data = (
                json.dumps(trajectory, ensure_ascii=False, indent=2) + "\n"
            ).encode()
            write_same(bundle / "normalized" / problem / "trajectory.json", data)
            if end.get("graded") and complete:
                eligible[problem] = data
        captured += complete
        rows.append(
            {
                **end.get("native_result", {}),
                "problem_id": problem,
                "campaign": run_name,
                "session_id": session or "",
                "graded": end.get("graded", False),
                "guardian_intervened": end.get("intervened", ""),
                "capture_complete": complete,
                "cleanup_verified": end.get("cleanup_verified", ""),
                "recovered": end.get("recovered", ""),
            }
        )
    coverage = {
        "selected": len(campaign["problems"]),
        "started": len(starts),
        "closed": len(ends),
        "graded": sum(bool(e["graded"]) for e in ends.values()),
        "unscored_closed": sum(not e["graded"] for e in ends.values()),
        "intervened": sum(e["kind"] == "intervention" for e in events),
        "unresolved": len(starts - ends.keys()),
        "fully_captured": captured,
        "eligible_for_sharing": len(eligible),
    }
    text = io.StringIO(newline="")
    fields = list(
        dict.fromkeys(
            [
                "campaign",
                "problem_id",
                "session_id",
                "graded",
                "guardian_intervened",
                "capture_complete",
                "cleanup_verified",
                "recovered",
            ]
            + sorted({k for row in rows for k in row})
        )
    )
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    csv_data = text.getvalue().encode()
    (bundle / "results.csv").write_bytes(csv_data)
    (bundle / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
    deployment = campaign["deployment"]
    collected_at = datetime.now(timezone.utc).isoformat()
    readme = (
        f"""# {run_name}\n\nCollected {collected_at}.\n\nNormalized HolmesGPT/SREGym trajectories, derived offline from private native SDK records and a labeled integration sidecar. These are not native HolmesGPT ATIF files. No automatic redaction is applied; review content before publishing.\n\nEach problem links to this campaign's results.csv by problem_id and session_id. A negative native grade is a valid result. A guardian intervention is unscored even if a late native grade exists. Capture completeness and native grade are separate. Only graded, fully captured trajectories appear here; results.csv retains accounting for other started attempts.\n\nAgent: {deployment["model"]}; judge: {deployment["judge_model"]}. Settings: {json.dumps(campaign["settings"], sort_keys=True)}. Supervisor: {campaign["supervisor_revision"]}; agent image revision: {campaign["image_revision"]}; image: {campaign["image_id"]}. Upstream source pins: {json.dumps(deployment["source_pins"], sort_keys=True)}. Named patches are listed in patches/README.md at the supervisor revision. Tool/dependency configuration: {json.dumps(deployment.get("versions", {}), sort_keys=True)}.\n\n| Coverage | Count |\n| --- | ---: |\n"""
        + "".join(f"| {k} | {v} |\n" for k, v in coverage.items())
    )
    return eligible, csv_data, readme.encode(), coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--share",
        action="store_true",
        help="Also write eligible exports under trajectories/run-NN; review before publishing",
    )
    args = parser.parse_args()
    os.umask(0o077)
    run = valid_run(args.run)
    config = load_config()
    node = config["nodes"][0]
    destination = ROOT / "private/collected" / run
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=run + "-", dir=destination.parent))
    command = f"cd {shlex.quote(config['remote_root'])} && python3 -c {shlex.quote(PACKAGE)} {shlex.quote(run)}"
    with subprocess.Popen(
        [*ssh_command(node["alias"], config.get("ssh_key"), node["user"]), command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        counts = receive(process, staging)
    merge(staging, destination)
    shutil.rmtree(staging)  # Only this successful transfer's temporary copy.
    eligible, csv_data, readme, coverage = outputs(destination, run)
    if args.share:
        target = ROOT / "trajectories" / run
        for problem, data in eligible.items():
            write_same(target / problem / "trajectory.json", data)
        # Snapshot summaries may advance as the idle campaign is later resumed.
        for name, data in [("results.csv", csv_data), ("README.md", readme)]:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print(
        f"Collected {counts['files']} files / {counts['bytes']:,} bytes into {destination}"
    )
    print(json.dumps(coverage, indent=2))
    print(
        "Sharing exports written; review their content before publishing."
        if args.share
        else "Private collection only. Use --share explicitly to create sharing exports."
    )


if __name__ == "__main__":
    main()
