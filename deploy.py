#!/usr/bin/env python3
"""Interactively prepare three fresh CloudLab nodes and the pinned benchmark runtime."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from runtime.remote import ROOT, CONFIG, remote_python, ssh, ssh_command
from runtime.sources import check_canonical, git

PROBE = r"""
import json,os,platform,shutil,subprocess
from pathlib import Path
r=json.load(__import__('sys').stdin)
def output(args):return subprocess.check_output(args,text=True).strip()
subprocess.run(['sudo','-n','true'],check=True)
interfaces=json.loads(output(['ip','-j','-4','address']))
addresses={a['local']:i['ifname'] for i in interfaces for a in i.get('addr_info',[])}
private_ip=r['private_ip']
if private_ip not in addresses:raise RuntimeError('Requested private address is not assigned')
for peer in r['peers']:
 route=json.loads(output(['ip','-j','route','get',peer,'from',private_ip]))[0]
 if route.get('dev')!=addresses[private_ip]:raise RuntimeError('Peer traffic leaves the selected private interface')
 subprocess.run(['ping','-q','-I',private_ip,'-c','2','-W','2',peer],check=True,stdout=subprocess.DEVNULL)
 import socket
 with socket.socket() as connection:
  connection.settimeout(5);connection.bind((private_ip,0));connection.connect((peer,22))
  if not connection.recv(100).startswith(b'SSH-'):raise RuntimeError('Missing private SSH banner')
mem=dict(l.split(':',1) for l in Path('/proc/meminfo').read_text().splitlines())
root=Path(r['remote_root']).expanduser()
existing=Path('/etc/kubernetes/kubelet.conf').exists() or Path('/etc/kubernetes/admin.conf').exists()
active=False
lock_path=root/'private/campaign.lock'
if lock_path.exists():
 import fcntl
 with lock_path.open('a') as handle:
  try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:active=True
if (root/'.git').exists():
 dirty=output(['git','-C',str(root),'status','--porcelain','--untracked-files=all','--ignore-submodules=none'])
 if dirty:raise RuntimeError('Parent/canonical source has edits; preserve and inspect it before provisioning')
if existing:
 for path in (Path.home()/'.kube/config',Path('/etc/kubernetes/kubelet.conf')):
  if not path.exists():continue
  prefix=['sudo','-n'] if str(path).startswith('/etc/') else []
  view=json.loads(output(prefix+['kubectl','--kubeconfig='+str(path),'config','view','--minify','-o','json']))
  if view['clusters'][0]['cluster']['server']!='https://'+r['control_ip']+':6443':
   raise RuntimeError('Existing kubeconfig selects another control plane; preserve it and choose the correct environment')
if existing and Path.home().joinpath('.kube/config').exists():
 controllers=json.loads(output(['kubectl','--request-timeout=8s','get','deployments,statefulsets,daemonsets,jobs,cronjobs,replicationcontrollers','-A','-o','json']))['items']
 protected={'kube-system','kube-public','kube-node-lease','openebs','sregym','chaos-mesh'}
 if any(item['metadata'].get('namespace','default') not in protected for item in controllers):
  raise RuntimeError('Unexpected workload controllers remain; recover their owner before deployment')

print(json.dumps({'home':str(Path.home()),'user':output(['id','-un']),
 'hostname':output(['hostname']),'machine_id':Path('/etc/machine-id').read_text().strip(),
 'system':platform.system(),'architecture':platform.machine(),'os_release':Path('/etc/os-release').read_text(),
 'private_ip':private_ip,'interface':addresses[private_ip],'disk_free':shutil.disk_usage('/').free,
 'memory_available':int(mem['MemAvailable'].split()[0])*1024,'kernel':platform.release(),
 'cluster_present':existing,'campaign_active':active,'managed':(root/'private/deployment.json').is_file(),
 'key_present':Path(r['api_key_file']).expanduser().is_file(),
 'docker_present':shutil.which('docker') is not None,
 'installed_packages':subprocess.run(['dpkg-query','-W','-f=${Package}=${Version}\\n','docker-ce','kubeadm','kubelet','kubectl'],capture_output=True,text=True).stdout,
 'docker_cgroup_options':json.loads(Path('/etc/docker/daemon.json').read_text()).get('exec-opts') if Path('/etc/docker/daemon.json').exists() else None}))
"""


def prompt(label, default):
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def stream(command, log, label, env=None, stdin=None):
    print(f"\n{label}", flush=True)
    with log.open("ab") as output:
        process = subprocess.Popen(
            command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = b""
        last_update = time.monotonic()
        while selector.get_map():
            ready = selector.select(timeout=20)
            if time.monotonic() - last_update > 20:
                print(f"  Still working: {label}", flush=True)
                last_update = time.monotonic()
            for key, _ in ready:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    pending = b""
                    continue
                output.write(chunk)
                output.flush()
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    text = line.decode(errors="replace").rstrip()
                    if text.startswith(
                        (
                            "PLAY ",
                            "PLAY RECAP",
                            "TASK ",
                            "fatal:",
                            "FAILED",
                            "+ ",
                            "Applying ",
                            "Downloading ",
                            "Native ",
                            "SDK",
                            "Node0 ",
                            "ERROR",
                            "Three-node ",
                            "All six ",
                            "Waiting for recovery:",
                        )
                    ):
                        print(text, flush=True)
        if process.wait():
            raise RuntimeError(f"{label} failed; preserved log: {log}")


def acquire(config, log):
    """Transfer only committed parent Git history; fetch upstream sources publicly."""
    bundle = ROOT / "private/deployment.bundle"
    subprocess.run(["git", "bundle", "create", str(bundle), "HEAD"], cwd=ROOT, check=True)
    code = r"""
import os,sys,json,subprocess,tempfile
from pathlib import Path
root=Path(sys.argv[1]);revision=sys.argv[2]
temporary=Path(tempfile.mkdtemp(prefix='holmes-source-'))/'parent.bundle'
temporary.write_bytes(sys.stdin.buffer.read());temporary.chmod(0o600)
def run(args):subprocess.run(args,check=True)
def git(*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
if root.exists():
 if not (root/'.git').exists():raise RuntimeError('Destination exists and is not this repository; choose a new directory')
 if git('status','--porcelain','--untracked-files=all'):raise RuntimeError('Destination has edits; preserve and inspect it')
 for line in git('submodule','status','--recursive').splitlines():
  if line.startswith(('U','+')):raise RuntimeError('Unexpected submodule state')
 run(['git','-C',str(root),'fetch',str(temporary),'HEAD'])
 run(['git','-C',str(root),'checkout','--detach',revision])
else:
 run(['git','clone',str(temporary),str(root)])
 run(['git','-C',str(root),'checkout','--detach',revision])
run(['git','-C',str(root),'submodule','update','--init','--recursive','--checkout'])
(root/'private').mkdir(mode=0o700,exist_ok=True)
print('Committed parent and recursive public sources ready',flush=True)
"""
    node = config["nodes"][0]
    command = (
        "python3 -c "
        + shlex.quote(code)
        + " "
        + shlex.join([config["remote_root"], config["parent_revision"]])
    )
    # The bundle contains no ignored private configuration or keys.
    with bundle.open("rb") as data:
        stream(
            [*ssh_command(node["alias"], config["ssh_key"], node["user"]), command],
            log,
            "Obtain the committed parent and recursive public sources",
            stdin=data,
        )
    print("Committed parent and recursive public sources ready", flush=True)


def credentials(config, local_key):
    node = config["nodes"][0]
    code = r"""
import os,sys
from pathlib import Path
path=Path(sys.argv[1]).expanduser();value=sys.stdin.buffer.read().strip()
if path.exists():
 if path.read_bytes().strip()!=value:raise RuntimeError('Remote credential differs; choose another path instead of overwriting it')
else:
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('xb') as handle:handle.write(value+b'\n')
path.chmod(0o600)
"""
    if local_key is not None:
        command = "python3 -c " + shlex.quote(code) + " " + shlex.quote(config["api_key_file"])
        ssh(node["alias"], command, data=local_key, key=config["ssh_key"], user=node["user"])
    check = 'from pathlib import Path; import os; p=Path(__import__("sys").argv[1]).expanduser(); assert p.is_file() and p.stat().st_uid==os.getuid() and p.stat().st_mode & 0o077==0 and p.read_text().strip(); print("Credential file available with private permissions")'
    print(
        ssh(
            node["alias"],
            "python3 -c " + shlex.quote(check) + " " + shlex.quote(config["api_key_file"]),
            key=config["ssh_key"],
            user=node["user"],
        )
        .decode()
        .strip()
    )


def worker_access(config):
    node0 = config["nodes"][0]
    key = config["ssh_key"]
    public = (
        ssh(
            node0["alias"],
            "cat " + shlex.quote(config["remote_root"] + "/private/worker_ed25519.pub"),
            key=key,
            user=node0["user"],
        )
        .decode()
        .strip()
    )
    verified = []
    for node in config["nodes"][1:]:
        host_key = (
            ssh(node["alias"], "cat /etc/ssh/ssh_host_ed25519_key.pub", key=key, user=node["user"])
            .decode()
            .strip()
        )
        verified.append(node["private_ip"] + " " + " ".join(host_key.split()[:2]))
        code = r"""
import json,os
from pathlib import Path
r=json.load(__import__('sys').stdin);directory=Path.home()/'.ssh';directory.mkdir(mode=0o700,exist_ok=True)
path=directory/'authorized_keys';line='restrict,from="'+r['source']+'" '+r['public']
old=path.read_text() if path.exists() else ''
if line not in old.splitlines():
 with path.open('a') as handle:handle.write(('\n' if old and not old.endswith('\n') else '')+line+'\n')
path.chmod(0o600)
"""
        remote_python(
            node["alias"],
            code,
            {"source": node0["private_ip"], "public": public},
            key=key,
            user=node["user"],
        )
    code = r"""
import json,subprocess
from pathlib import Path
lines=json.load(__import__('sys').stdin);directory=Path.home()/'.ssh';directory.mkdir(mode=0o700,exist_ok=True)
path=directory/'known_hosts';old=path.read_text() if path.exists() else ''
for line in lines:
 host=line.split()[0]
 existing=subprocess.run(['ssh-keygen','-F',host,'-f',str(path)],capture_output=True,text=True).stdout if path.exists() else ''
 keys=[' '.join(x.split()[1:3]) for x in existing.splitlines() if x and not x.startswith('#')]
 if keys and ' '.join(line.split()[1:3]) not in keys:raise RuntimeError('Existing worker host key differs; preserve and inspect known_hosts')
 if line not in old.splitlines():
  with path.open('a') as handle:handle.write(line+'\n')
path.chmod(0o600)
"""
    remote_python(node0["alias"], code, verified, key=key, user=node0["user"])
    print(
        "Restricted worker key authorized; host keys verified through existing authenticated SSH",
        flush=True,
    )
    code = (
        "import os; os.environ['SREGYM_WORKER_SSH_KEY']="
        + repr(config["remote_root"] + "/private/worker_ed25519")
        + "; "
        "from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector; "
        "injector=RemoteOSFaultInjector(); workers=injector._get_remote_worker_info(); assert len(workers)==2; "
        "assert all(injector._ssh_exec(host,user,'sudo -n true && printf native-worker-ssh-ok')=='native-worker-ssh-ok' for host,user in workers.items()); "
        "print('Native fault-injection SSH and sudo path passed without injecting a fault')"
    )
    command = (
        "cd "
        + shlex.quote(config["remote_root"] + "/.work/checkouts/SREGym")
        + " && "
        + shlex.quote(config["remote_root"] + "/.work/host/bin/python")
        + " -c "
        + shlex.quote(code)
    )
    print(
        ssh(node0["alias"], command, key=key, user=node0["user"], timeout=45).decode().strip(),
        flush=True,
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", nargs=3, metavar=("CONTROL", "WORKER1", "WORKER2"))
    parser.add_argument("--private-ips", nargs=3)
    parser.add_argument("--private-cidr", default="10.10.1.0/24")
    parser.add_argument("--remote-root", default="~/holmesgpt-sregym")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--user", help="Override SSH-configured login user on all nodes")
    parser.add_argument(
        "--api-key-file", type=Path, help="Local credential file; contents never printed"
    )
    parser.add_argument("--remote-api-key-file", default="~/.sregym.anthropic_api_key")
    parser.add_argument("--model", default="anthropic/claude-sonnet-5-5")
    parser.add_argument("--judge-model")
    parser.add_argument("--ansible", default="ansible-playbook")
    parser.add_argument("--example", action="store_true", help="After setup, start one unchanged incorrect_image example in tmux")
    parser.add_argument(
        "--yes", action="store_true", help="Use supplied/default values and proceed without prompts"
    )
    args = parser.parse_args()
    print(
        "node0: Kubernetes control plane, benchmark supervisor and result storage.\nnode1/node2: workers. Cluster traffic uses the private experiment LAN.\nSetup checks SSH/LAN, installs a matching kernel (reboot if needed), Docker and Kubernetes,\nbuilds the disclosed SDK variant, and checks recovery headroom. No full campaign starts automatically."
    )
    if not args.yes:
        user = prompt("SSH login user (or use SSH configuration)", args.user or "SSH config")
        args.user = None if user == "SSH config" else user
        identity = prompt(
            "Local SSH key path (never copied to nodes)",
            str(args.ssh_key) if args.ssh_key else "SSH config/agent",
        )
        args.ssh_key = None if identity == "SSH config/agent" else Path(identity).expanduser()
        args.remote_root = prompt("Node0 installation directory", args.remote_root)
        args.model = prompt("Agent Anthropic model", args.model)
        args.judge_model = prompt("Judge Anthropic model", args.judge_model or args.model)
        args.remote_api_key_file = prompt("Node0 API-key file", args.remote_api_key_file)
    if any(
        not (model.startswith("anthropic/") or model.startswith("claude-"))
        for model in (args.model, args.judge_model or args.model)
    ):
        raise ValueError(
            "This entry point supports Anthropic model credentials; use an anthropic/ model ID"
        )
    aliases = args.nodes or (
        [prompt(f"Node {i} SSH alias/hostname", f"node{i}") for i in range(3)]
        if not args.yes
        else ["node0", "node1", "node2"]
    )
    ips = args.private_ips or (
        [prompt(f"Node {i} private IPv4", f"10.10.1.{i + 1}") for i in range(3)]
        if not args.yes
        else ["10.10.1.1", "10.10.1.2", "10.10.1.3"]
    )
    if len(set(aliases)) != 3 or len(set(ips)) != 3:
        raise ValueError("Provide three distinct nodes and three distinct private addresses")
    network = ipaddress.ip_network(args.private_cidr)
    if (
        network.version != 4
        or not network.is_private
        or any(ipaddress.ip_address(ip) not in network for ip in ips)
    ):
        raise ValueError("All selected addresses must belong to the private experiment subnet")
    if args.ssh_key:
        args.ssh_key = args.ssh_key.expanduser().resolve()
        if not args.ssh_key.is_file():
            raise ValueError("The selected SSH key file does not exist")
    check_canonical()
    if git(
        ROOT,
        "diff",
        "HEAD",
        "--",
        ".gitmodules",
        "upstream",
        "patches",
        "runtime/agent.py",
        "runtime/recording.py",
    ):
        raise RuntimeError("Commit reviewed source-pin, patch or agent changes before deployment")
    versions = tomllib.loads((ROOT / "versions.toml").read_text())
    version = subprocess.check_output([args.ansible, "--version"], text=True)
    if f"[core {versions['ansible_core']}]" not in version:
        raise RuntimeError(
            "Install the locked controller requirements and select that ansible-playbook with --ansible"
        )
    private = ROOT / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    log = private / ("deploy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".log")
    nodes = []
    for i, (alias, ip) in enumerate(zip(aliases, ips)):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias):
            raise ValueError(
                "Use a hostname or SSH alias; specify user separately or in ~/.ssh/config"
            )
        settings = subprocess.check_output(
            [
                "ssh",
                "-G",
                "-T",
                *(["-i", str(args.ssh_key)] if args.ssh_key else []),
                *(["-l", args.user] if args.user else []),
                alias,
            ],
            text=True,
        )
        resolved = dict(line.split(" ", 1) for line in settings.splitlines())
        payload = {
            "private_ip": ip,
            "control_ip": ips[0],
            "peers": [p for p in ips if p != ip],
            "remote_root": args.remote_root,
            "api_key_file": args.remote_api_key_file,
        }
        health = json.loads(
            remote_python(alias, PROBE, payload, key=args.ssh_key, user=resolved["user"])
        )
        if health.get("campaign_active"):
            raise RuntimeError(f"{alias}: a campaign owns the cluster; do not deploy during a run")
        if 'VERSION_ID="22.04"' not in health["os_release"] or health["architecture"] != "x86_64":
            raise RuntimeError(f"{alias}: requires Ubuntu 22.04 x86-64")
        if health["disk_free"] < 30 * 1024**3 or health["memory_available"] < 16 * 1024**3:
            raise RuntimeError(
                f"{alias}: insufficient initial headroom; free resources without changing benchmark settings"
            )
        expected_packages = {
            "docker-ce": versions["docker_package"],
            **{n: versions["kubernetes"] + "-1.1" for n in ("kubeadm", "kubelet", "kubectl")},
        }
        for installed in health.get("installed_packages", "").splitlines():
            name, value = installed.split("=", 1)
            if value and name in expected_packages and value != expected_packages[name]:
                raise RuntimeError(
                    f"{alias}: {name}={value} conflicts with the frozen setup; no automatic replacement"
                )
        if health.get("docker_present"):
            if health.get("docker_cgroup_options") != ["native.cgroupdriver=systemd"]:
                raise RuntimeError(
                    f"{alias}: preserve and inspect the existing Docker configuration before setup"
                )
        nodes.append(
            {
                "alias": alias,
                "user": resolved["user"],
                "port": int(resolved["port"]),
                "private_ip": ip,
                **health,
            }
        )
        print(
            f"{alias}: {health['hostname']}, private {ip}, RAM available {health['memory_available'] / 1024**3:.1f} GiB, disk free {health['disk_free'] / 1024**3:.1f} GiB",
            flush=True,
        )
    if len({n["machine_id"] for n in nodes}) != 3:
        raise RuntimeError("Aliases resolve to fewer than three physical nodes")
    previous = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    same_hosts = [n["machine_id"] for n in nodes] == [
        n.get("machine_id") for n in previous.get("nodes", [])
    ]
    if any(n["cluster_present"] for n in nodes) and not nodes[0]["managed"] and not same_hosts:
        raise RuntimeError(
            "An existing cluster is not marked as managed by this deployment; refusing to overwrite it"
        )
    remote_root = (
        args.remote_root.replace("~", nodes[0]["home"], 1)
        if args.remote_root.startswith("~/")
        else args.remote_root
    )
    if not Path(remote_root).is_absolute():
        raise ValueError("--remote-root must be absolute or start with ~/")
    key = args.api_key_file.expanduser().read_bytes().strip() if args.api_key_file else None
    if key is None and not nodes[0]["key_present"]:
        key = os.environ.get("ANTHROPIC_API_KEY", "").encode().strip() or None
        if key is None and not args.yes:
            source = prompt("Local API-key file (leave default for hidden input)", "hidden")
            key = (
                getpass.getpass("Anthropic API key: ").encode().strip()
                if source == "hidden"
                else Path(source).expanduser().read_bytes().strip()
            )
        if not key:
            raise RuntimeError(
                "Provide --api-key-file, ANTHROPIC_API_KEY or a valid remote key file"
            )
    config = {
        "nodes": nodes,
        "remote_root": remote_root,
        "ssh_key": str(args.ssh_key.resolve()) if args.ssh_key else None,
        "api_key_file": args.remote_api_key_file,
        "private_cidr": str(network),
        "versions": versions,
        "model": args.model,
        "judge_model": args.judge_model or args.model,
        "parent_revision": git(ROOT, "rev-parse", "HEAD"),
    }
    print(
        f"\nRuntime parent: {config['parent_revision']}\nInstall directory: {remote_root}\nKernel: {versions['kernel_release']}; Kubernetes: {versions['kubernetes']}; Calico: {versions['calico']}\nExpected fresh setup: 30–90 minutes. Kernel changes reboot only nodes needing the selected kernel.\nLogs remain private; existing keys/configuration/results are preserved."
    )
    print(
        f"Agent model: {config['model']}; judge: {config['judge_model']}; full profile, 100 SDK iterations per stage."
    )
    print("Named changes (details in patches/README.md):")
    for line in (ROOT / "patches/README.md").read_text().splitlines():
        if line.startswith("| `") and ".patch`" in line:
            columns = [c.strip() for c in line.strip("|").split("|")]
            print(f"  {columns[0].strip(chr(96))}: {columns[1]}")
    if not args.yes and prompt("Proceed with these changes? yes/no", "yes").lower() != "yes":
        return
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    inventory = {
        "all": {
            "vars": {
                **versions,
                "private_cidr": str(network),
                "ansible_ssh_common_args": "-o BatchMode=yes -o StrictHostKeyChecking=yes",
                "ansible_python_interpreter": "/usr/bin/python3",
            },
            "children": {"control_nodes": {"hosts": {}}, "worker_nodes": {"hosts": {}}},
        }
    }
    for i, node in enumerate(nodes):
        variables = {
            "ansible_host": node["alias"],
            "ansible_user": node["user"],
            "ansible_port": node["port"],
            "login_user": node["user"],
            "remote_home": node["home"],
            "private_ip": node["private_ip"],
        }
        if args.ssh_key:
            variables["ansible_ssh_private_key_file"] = str(args.ssh_key.resolve())
        inventory["all"]["children"]["control_nodes" if i == 0 else "worker_nodes"]["hosts"][
            f"node{i}"
        ] = variables
    inv = private / "inventory.json"
    inv.write_text(json.dumps(inventory, indent=2) + "\n")
    stream(
        [args.ansible, "-i", str(inv), str(ROOT / "ansible/cluster.yml")],
        log,
        "Prepare hosts and private Kubernetes",
        env={
            **os.environ,
            "ANSIBLE_LOCAL_TEMP": str(private / "ansible-tmp"),
            "ANSIBLE_HOST_KEY_CHECKING": "True",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
        },
    )
    acquire(config, log)
    node0 = nodes[0]
    for name, payload in [
        ("deployment-input.json", json.dumps(config, indent=2).encode()),
        ("bootstrap.py", (ROOT / "runtime/install.py").read_bytes()),
        ("health.py", (ROOT / "runtime/health.py").read_bytes()),
    ]:
        destination = remote_root + "/private/" + name
        code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o600)"
        ssh(
            node0["alias"],
            "python3 -c " + shlex.quote(code) + " " + shlex.quote(destination),
            data=payload,
            key=config["ssh_key"],
            user=node0["user"],
        )
    credentials(config, key)
    command = f"cd {shlex.quote(remote_root)} && PYTHONPATH={shlex.quote(remote_root)} python3 private/bootstrap.py private/deployment-input.json"
    stream(
        [*ssh_command(node0["alias"], config["ssh_key"], node0["user"]), command],
        log,
        "Prepare exact sources, dependencies and agent image",
    )
    worker_access(config)
    command = f"cd {shlex.quote(remote_root)} && .work/host/bin/python private/health.py private/deployment.json --prepare --network-check"
    stream(
        [*ssh_command(node0["alias"], config["ssh_key"], node0["user"]), command],
        log,
        "Validate the private cluster and resource reserves",
    )
    print(f"\nDeployment preparation finished. Detailed log: {log}", flush=True)
    example = args.example or (not args.yes and prompt("Start the paid incorrect_image example in tmux? yes/no", "no").lower() == "yes")
    if example:
        subprocess.run([sys.executable, str(ROOT / "run.py"), "--problem", "incorrect_image"], check=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        sys.exit(1)
