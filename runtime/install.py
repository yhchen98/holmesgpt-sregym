"""Prepare node0 using the committed sources, patches and dependency locks."""

import argparse
import io
import json
import os
import shlex
import subprocess
import tarfile
import urllib.request
from pathlib import Path


def command(args, **kwargs):
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def binary(url, member, target):
    if target.exists():
        return
    print(f"Downloading {url}", flush=True)
    data = urllib.request.urlopen(url, timeout=90).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        with archive.extractfile(member) as source:
            payload = source.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
    target.chmod(0o755)


def main(config):
    os.umask(0o077)
    root = Path(config["remote_root"])
    os.chdir(root)
    versions = config["versions"]
    bin_dir = root / ".work/bin"
    uv = bin_dir / "uv"
    binary(
        f"https://github.com/astral-sh/uv/releases/download/{versions['uv']}/uv-x86_64-unknown-linux-gnu.tar.gz",
        "uv-x86_64-unknown-linux-gnu/uv",
        uv,
    )
    binary(
        f"https://get.helm.sh/helm-v{versions['helm']}-linux-amd64.tar.gz",
        "linux-amd64/helm",
        bin_dir / "helm",
    )
    assert versions["uv"] == subprocess.check_output([uv, "--version"], text=True).split()[1]
    assert versions["helm"] == subprocess.check_output(
        [bin_dir / "helm", "version", "--short"], text=True
    ).strip().split("+")[0].removeprefix("v")
    os.environ.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        UV_CACHE_DIR=str(root / ".work/uv-cache"),
        UV_PYTHON_INSTALL_DIR=str(root / ".work/python"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    host = root / ".work/host"
    if not host.exists():
        command([uv, "venv", "--python", versions["python"], host])
    python = host / "bin/python"
    actual_python = subprocess.check_output(
        [python, "-c", "import platform; print(platform.python_version())"], text=True
    ).strip()
    if actual_python != versions["python"]:
        raise RuntimeError(
            "Existing host environment uses a different Python; preserve it and choose a new install directory"
        )
    command([uv, "pip", "sync", "--python", python, root / "images/host.lock"])
    command([uv, "pip", "check", "--python", python])
    from runtime.sources import check_canonical, prepare, verify_runtime, git

    check_canonical(root)
    checkouts = root / ".work/checkouts"
    if not checkouts.exists():
        prepare(checkouts, root)
    else:
        for name in ("SREGym", "holmesgpt"):
            verify_runtime(name, checkouts / name, root)
    # This inventory is native runtime configuration, never a source modification.
    inventory = {
        "all": {
            "vars": {},
            "children": {
                "worker_nodes": {
                    "hosts": {
                        f"worker_{i}": {"ansible_host": n["private_ip"], "ansible_user": n["user"]}
                        for i, n in enumerate(config["nodes"][1:], 1)
                    }
                }
            },
        }
    }
    inv_path = checkouts / "SREGym/scripts/ansible/inventory.yml"
    text = json.dumps(inventory, indent=2) + "\n"  # JSON is valid YAML.
    if inv_path.exists() and inv_path.read_text() != text:
        raise RuntimeError(f"Existing native inventory differs; preserve and inspect {inv_path}")
    if not inv_path.exists():
        inv_path.write_text(text)
    exclude = checkouts / "SREGym/.git/info/exclude"
    line = "/scripts/ansible/inventory.yml\n"
    if line not in exclude.read_text():
        with exclude.open("a") as handle:
            handle.write(line)
    worker_key = root / "private/worker_ed25519"
    if not worker_key.exists():
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", worker_key])
    worker_key.chmod(0o600)
    command(
        [
            python,
            "-c",
            "import main; from sregym.conductor.problems.registry import ProblemRegistry; "
            'assert len(ProblemRegistry().get_problem_ids()) == 217; print("Native 217-case registry/import passed")',
        ],
        cwd=checkouts / "SREGym",
    )
    revision = git(root, "rev-parse", "HEAD")
    image = f"holmes-sregym-agent:{revision[:12]}-patched"
    existing = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    if existing.returncode:
        command([python, "-m", "images.build", "--checkouts", checkouts, "--tag", image])
    else:
        labels = json.loads(existing.stdout)[0]["Config"].get("Labels") or {}
        if labels.get("org.opencontainers.image.revision") != revision:
            raise RuntimeError(f"Image label does not match its committed parent: {image}")
        from images.build import preflight

        preflight(image)
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    config.update(
        parent_revision=revision,
        image=image,
        image_id=info["Id"],
        worker_key=str(worker_key),
        source_pins=check_canonical(root),
        kubeconfig=str(Path.home() / ".kube/config"),
    )
    (root / "private/deployment.json").write_text(json.dumps(config, indent=2) + "\n")
    print(f"Node0 runtime prepared: {image}; host={host}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    main(json.loads(args.config.read_text()))
