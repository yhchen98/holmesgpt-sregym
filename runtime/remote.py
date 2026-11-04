"""Small SSH helpers shared by local entry points; honor normal SSH configuration."""

import json
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "private/config.json"


def ssh_command(alias, key=None, user=None):
    args = [
        "ssh",
        "-T",
        "-o",
        "ControlPath=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if key:
        args += ["-i", str(key)]
    if user:
        args += ["-l", user]
    return [*args, alias]


def ssh(alias, command, *, data=None, key=None, user=None, timeout=60):
    result = subprocess.run(
        [*ssh_command(alias, key, user), command], input=data, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"SSH command failed on {alias}: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def remote_python(alias, source, payload=None, **kwargs):
    command = "python3 -c " + shlex.quote(source)
    return ssh(
        alias, command, data=json.dumps(payload).encode() if payload is not None else None, **kwargs
    )


def load_config():
    if not CONFIG.exists():
        raise RuntimeError("Run python3 deploy.py first (private/config.json is missing)")
    return json.loads(CONFIG.read_text())
