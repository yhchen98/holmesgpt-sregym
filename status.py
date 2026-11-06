#!/usr/bin/env python3
"""Read the latest campaign status on node0; never repair or restart anything."""

import argparse
import shlex
from runtime.remote import load_config, ssh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="Default: latest numbered run")
    args = parser.parse_args()
    config = load_config()
    node = config["nodes"][0]
    from runtime.campaign import valid_run

    run = valid_run(args.run) if args.run else None
    code = r"""
import json,time,fcntl
from pathlib import Path
r=Path('private/runs');name=__import__('sys').argv[1]
p=r/name if name else max(r.glob('run-*'),key=lambda p:int(p.name[4:]))
def lines(path):return [json.loads(l) for l in path.read_bytes().splitlines(keepends=True) if l.endswith(b'\n')]
c=json.loads((p/'campaign.json').read_text());events=lines(p/'journal.jsonl') if (p/'journal.jsonl').exists() else []
ends=[e for e in events if e['kind']=='case_end'];active=json.loads((p/'active.json').read_text()) if (p/'active.json').exists() else None
running=False;lock=Path('private/campaign.lock')
if lock.exists():
 h=lock.open('r')
 try:fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:running=True
result={'run':p.name,'supervisor_active':running,'selected':len(c['problems']),'graded':sum(e['graded'] for e in ends),'unscored':sum(not e['graded'] for e in ends),'pending':len(c['problems'])-len(ends),'last_event':events[-1] if events else None,'active':active,'logs':str(p.resolve())}
for file in ('health.json','observation.json'):
 if (p/file).exists():
  v=json.loads((p/file).read_text());result[file]={'age_seconds':max(0,time.time()-(p/file).stat().st_mtime),'errors':v.get('errors',v.get('reasons',[]))}
if active:
 case=Path(active['case']);phases=list(case.glob('results/holmesgpt/*/phases_attempt1.jsonl'));result['last_phase']=(lines(phases[0]) or [None])[-1] if phases else None
 overlays=list(case.glob('results/**/overlay/records.jsonl'))
 if overlays:
  rows=lines(overlays[0])
  completions=[v for v in rows if v.get('kind')=='completion'];result['last_model_response']=completions[-1].get('recorded_at') if completions else None
print(json.dumps(result,indent=2))
"""
    command = f"cd {shlex.quote(config['remote_root'])} && sudo -n python3 -c {shlex.quote(code)} {shlex.quote(run or '')}"
    print(
        ssh(node["alias"], command, key=config.get("ssh_key"), user=node["user"])
        .decode()
        .strip()
    )


if __name__ == "__main__":
    main()
