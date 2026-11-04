"""Resolve gitlinks and prepare independent, visibly patched runtime sources."""
import argparse
import json
import os
import tempfile
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def pins(root=ROOT):
    entries = git(root, "ls-tree", "HEAD", "upstream/").splitlines()
    result = {}
    for line in entries:
        mode, kind, revision, name = line.split(maxsplit=3)
        if mode != "160000" or kind != "commit":
            raise RuntimeError(f"Not a submodule: {line}")
        result[Path(name).name] = revision
    if set(result) != {"SREGym", "holmesgpt"}:
        raise RuntimeError("Expected the two recorded upstream gitlinks")
    return result


def check_canonical(root=ROOT):
    expected = pins(root)
    for name, revision in expected.items():
        path = root / "upstream" / name
        if git(path, "status", "--porcelain", "--untracked-files=all"):
            raise RuntimeError(f"Canonical checkout is dirty; preserve and inspect it: {path}")
        if git(path, "rev-parse", "HEAD") != revision:
            raise RuntimeError(f"Canonical checkout differs from recorded gitlink: {path}")
        nested = git(path, "submodule", "status", "--recursive")
        if any(line.startswith(("-", "+", "U")) for line in nested.splitlines()):
            raise RuntimeError(f"Nested source revisions do not match: {path}")
    return expected


def verify_runtime(name, target, root=ROOT):
    """Compare the entire checkout diff with only the named, recorded patches."""
    target = Path(target)
    if git(target, "rev-parse", "HEAD") != pins(root)[name]:
        raise RuntimeError(f"Runtime source revision mismatch: {target}")
    if git(target, "ls-files", "--others", "--exclude-standard"):
        raise RuntimeError(f"Unexpected runtime source files: {target}")
    group = "sregym" if name == "SREGym" else "holmesgpt"
    with tempfile.TemporaryDirectory(prefix="holmes-patch-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        command = ["git", "-C", str(target)]
        subprocess.run([*command, "read-tree", "HEAD"], env=env, check=True)
        for patch in sorted((root / "patches" / group).glob("*.patch")):
            subprocess.run([*command, "apply", "--cached", str(patch)], env=env, check=True)
        expected = subprocess.check_output([*command, "diff", "--cached", "--no-ext-diff", "--no-color", "--binary", "HEAD"], env=env)
    actual = subprocess.check_output(["git", "-C", str(target), "diff", "--no-ext-diff", "--no-color", "--binary", "HEAD"])
    if actual != expected:
        raise RuntimeError(f"Runtime edits differ from the named patch set; preserve and inspect: {target}")


def prepare(destination, root=ROOT):
    expected = check_canonical(root)
    destination = Path(destination).resolve()
    for name, revision in expected.items():
        canonical = root / "upstream" / name
        target = destination / name
        patch_dir = root / "patches" / ("sregym" if name == "SREGym" else "holmesgpt")
        patches = sorted(patch_dir.glob("*.patch"))
        if target.exists():
            raise RuntimeError(f"Build checkout already exists; choose a new path: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Independent object store; changing this checkout cannot alter the reference.
        subprocess.run(["git", "clone", "--no-hardlinks", str(canonical), str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "--detach", revision], check=True)
        subprocess.run(["git", "-C", str(target), "submodule", "update", "--init", "--recursive", "--checkout"], check=True)
        for patch in patches:
            print(f"Applying {patch.relative_to(root)} (see patches/README.md)", flush=True)
            subprocess.run(["git", "-C", str(target), "apply", "--check", str(patch)], check=True)
            subprocess.run(["git", "-C", str(target), "apply", str(patch)], check=True)
    for name in expected:
        verify_runtime(name, destination / name, root)
    check_canonical(root)
    return expected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.prepare) if args.prepare else check_canonical(), indent=2))
