"""Build only from the recorded gitlinks, reviewed patches and locked dependencies."""

import argparse
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from runtime.sources import ROOT, check_canonical, verify_runtime, git


def build(checkouts, tag):
    check_canonical()
    if git(ROOT, "status", "--porcelain"):
        raise RuntimeError("Commit or preserve parent source edits before building a release image")
    checkouts = Path(checkouts)
    sdk = checkouts / "holmesgpt"
    verify_runtime("holmesgpt", sdk)
    # A failed build remains inspectable. A retry never overwrites its context.
    (ROOT / ".work").mkdir(exist_ok=True)
    context = Path(tempfile.mkdtemp(prefix="image-context-", dir=ROOT / ".work"))
    (context / "runtime").mkdir(parents=True)
    for name in ("__init__.py", "agent.py", "recording.py"):
        shutil.copy2(ROOT / "runtime" / name, context / "runtime" / name)
    shutil.copytree(ROOT / "images", context / "images")
    shutil.copytree(
        sdk, context / "sdk", ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "tests")
    )
    versions = tomllib.loads((ROOT / "versions.toml").read_text())
    command = [
        "docker",
        "build",
        "--platform=linux/amd64",
        "-t",
        tag,
        "-f",
        str(context / "images/Dockerfile"),
    ]
    revision = git(ROOT, "rev-parse", "HEAD")
    command.extend(
        [
            "--build-arg",
            f"INTEGRATION_REVISION={revision}",
            "--label",
            f"org.opencontainers.image.revision={revision}",
        ]
    )
    for key in ("python_image", "debian_snapshot", "kubernetes"):
        command.extend(["--build-arg", f"{key.upper()}={versions[key]}"])
    subprocess.run([*command, str(context)], check=True)
    preflight(tag)


def preflight(tag):
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint=python",
            tag,
            "-c",
            "import holmes,holmes.config,runtime.agent; from importlib.metadata import version; "
            "print('SDK:',version('holmesgpt'),holmes.__file__); "
            "from pathlib import Path; import inspect; from holmes.plugins.toolsets.bash.common.bash import execute_bash_command; "
            "assert 'killpg' in inspect.getsource(execute_bash_command); "
            "assert not Path('/opt/sregym/sregym/conductor').exists(); "
            "assert {p.name for p in Path('/opt/sregym/runtime').glob('*.py')} == {'__init__.py','agent.py','recording.py'}; "
            "print('SDK image preflight OK')",
        ],
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkouts", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    build(args.checkouts, args.tag)
