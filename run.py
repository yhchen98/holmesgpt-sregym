#!/usr/bin/env python3
"""Start one native problem, a full campaign, or resume closed-case accounting in tmux."""

import argparse
import shlex
from runtime.remote import ROOT, load_config, ssh
from runtime.sources import git
from runtime.campaign import valid_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list",
        action="store_true",
        help="List the native problem registry without starting a run",
    )
    group.add_argument("--problem")
    group.add_argument("--all", action="store_true")
    group.add_argument("--resume", metavar="RUN")
    args = parser.parse_args()
    config = load_config()
    node = config["nodes"][0]
    root = config["remote_root"]
    q = shlex.quote

    def remote(command, timeout=60):
        return (
            ssh(
                node["alias"],
                command,
                key=config.get("ssh_key"),
                user=node["user"],
                timeout=timeout,
            )
            .decode()
            .strip()
        )

    if args.list:
        code = "from sregym.conductor.problems.registry import ProblemRegistry; print('\\n'.join(ProblemRegistry().get_problem_ids()))"
        print(
            remote(
                f"cd {q(root + '/.work/checkouts/SREGym')} && {q(root + '/.work/host/bin/python')} -c {q(code)}"
            )
        )
        return
    if git(ROOT, "status", "--porcelain"):
        raise RuntimeError("Commit/preserve source edits before launching")
    revision = remote(f"git -C {q(root)} rev-parse HEAD")
    if revision != git(ROOT, "rev-parse", "HEAD"):
        raise RuntimeError("Local and node0 releases differ; deploy this release first")
    check = "import fcntl; from pathlib import Path; p=Path('private/campaign.lock'); p.parent.mkdir(exist_ok=True); h=p.open('a'); fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    remote(f"cd {q(root)} && python3 -c {q(check)}")
    command = ".work/host/bin/python -m runtime.campaign"
    run = (
        valid_run(args.resume)
        if args.resume
        else remote(
            f"cd {q(root)} && {command} --create"
            + (f" --problem {q(args.problem)}" if args.problem else "")
        )
    )
    session = "holmes-" + run
    # tmux loads the user's existing configuration; never replace it or use -f.
    shell = f"cd {q(root)} && exec {command} --run {q(run)} >> private/runs/{run}/supervisor.log 2>&1"
    remote("tmux new-session -d -s " + q(session) + " " + q(shell))
    print(f"{run} started on {node['alias']}; tmux session {session}")
    print(f"Attach: ssh -t {node['alias']} tmux attach -t {session}")
    print("Status: python3 status.py --run " + run)


if __name__ == "__main__":
    main()
