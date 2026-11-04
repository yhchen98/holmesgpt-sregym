"""Bounded host/cluster observations and the deployment's temporary LAN probes."""

import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

HOST_SAMPLE = r"""
import json,os,subprocess,time
from pathlib import Path
m={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines()) if v.strip().split()[0].isdigit()}
pressure={}
for resource in ('cpu','memory','io'):
 pressure[resource]={line.split()[0]:dict((key,float(value)) for key,value in (field.split('=') for field in line.split()[1:]) if key!='total') for line in Path('/proc/pressure',resource).read_text().splitlines()}
disks={}
for path in ('/','/var/lib/docker'):
 stat=os.statvfs(path);disks[path]={'available':stat.f_bavail*stat.f_frsize,'free_inodes':stat.f_favail,'total_inodes':stat.f_files}
services={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True,timeout=3).stdout.strip() for name in ('docker','cri-docker','kubelet')}
print(json.dumps({'machine_id':Path('/etc/machine-id').read_text().strip(),'time':time.time(),'memory_available':m['MemAvailable'],'memory_total':m['MemTotal'],'swap_total':m['SwapTotal'],'load':os.getloadavg(),'cpus':os.cpu_count(),'pressure':pressure,'disks':disks,'services':services}))
"""


def kubectl(*args, timeout=15):
    return subprocess.check_output(
        ["kubectl", "--request-timeout=8s", *args], text=True, timeout=timeout
    ).strip()


def sample_all(config):
    def sample(index, node):
        started = time.monotonic()
        try:
            if index == 0:
                command = ["python3", "-c", HOST_SAMPLE]
            else:
                command = [
                    "ssh",
                    "-T",
                    "-i",
                    config["worker_key"],
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "ControlPath=none",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=5",
                    f"{node['user']}@{node['private_ip']}",
                    "python3 -c " + shlex.quote(HOST_SAMPLE),
                ]
            result = json.loads(
                subprocess.check_output(command, timeout=15, stderr=subprocess.PIPE)
            )
            if result["machine_id"] != node["machine_id"]:
                raise ValueError("Host identity differs from deployment")
            return {
                "node": f"node{index}",
                "received_at": time.time(),
                "latency": time.monotonic() - started,
                "received_monotonic": time.monotonic(),
                **result,
            }
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"node": f"node{index}", "received_at": time.time(), "error": type(exc).__name__}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(sample, i, node) for i, node in enumerate(config["nodes"])]
        return [future.result() for future in futures]


def recovery_errors(samples):
    errors = []
    if {host.get("node") for host in samples} != {"node0", "node1", "node2"} or len(samples) != 3:
        errors.append("Three distinct host observations are required")
    for host in samples:
        name = host["node"]
        if host.get("error") or time.monotonic() - host.get("received_monotonic", 0) > 25:
            errors.append(f"{name}: missing or stale host observation")
            continue
        if host["memory_available"] < max(8 * 1024**3, 0.12 * host["memory_total"]):
            errors.append(f"{name}: insufficient RAM reserve")
        if host["swap_total"]:
            errors.append(f"{name}: swap is enabled")
        if (
            host["pressure"]["cpu"].get("some", {}).get("avg10", 0) > 70
            and host["load"][0] > host["cpus"] * 0.8
        ):
            errors.append(f"{name}: insufficient recovered CPU headroom")
        for disk in host["disks"].values():
            if disk["available"] < 15 * 1024**3 or disk["free_inodes"] < 0.1 * disk["total_inodes"]:
                errors.append(f"{name}: insufficient disk/inode reserve")
        if any(value != "active" for value in host["services"].values()):
            errors.append(f"{name}: required service inactive")
        if (
            host["pressure"]["memory"].get("full", {}).get("avg10", 0) > 2
            or host["pressure"]["io"].get("full", {}).get("avg10", 0) > 10
        ):
            errors.append(f"{name}: recovery pressure remains high")
    return errors


def cluster_errors(config):
    errors = []
    try:
        readyz = kubectl("get", "--raw=/readyz?verbose")
        if "readyz check passed" not in readyz:
            errors.append("API/etcd readiness failed")
        nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
        if {n["metadata"]["name"] for n in nodes} != {"node0", "node1", "node2"}:
            errors.append("Unexpected cluster node membership")
        for node in nodes:
            if not any(
                c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]
            ):
                errors.append(f"{node['metadata']['name']}: not Ready")
        pods = json.loads(kubectl("get", "pods", "-n", "kube-system", "-o", "json"))["items"]
        for pod in pods:
            if pod["status"].get("phase") == "Succeeded" and any(
                o["kind"] == "Job" for o in pod["metadata"].get("ownerReferences", [])
            ):
                continue
            if not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod["status"].get("conditions", [])
            ):
                errors.append(f"kube-system/{pod['metadata']['name']}: not Ready")
        metrics = json.loads(kubectl("get", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"))
        if len(metrics.get("items", [])) != 3:
            errors.append("Metrics API has not reported all three nodes")
        expected = {f"node{i}": n["private_ip"] for i, n in enumerate(config["nodes"])}
        # Kubernetes-datastore Calico stores node addresses on native Node annotations.
        if any(
            n["metadata"]
            .get("annotations", {})
            .get("projectcalico.org/IPv4Address", "")
            .split("/")[0]
            != expected.get(n["metadata"]["name"])
            for n in nodes
        ):
            errors.append("Calico advertises an unexpected interface")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        errors.append(f"Cluster check unavailable: {type(exc).__name__}")
    return errors


def prepare_metrics(config):
    import urllib.request
    import yaml

    version = config["versions"]["metrics_server"]
    url = f"https://github.com/kubernetes-sigs/metrics-server/releases/download/v{version}/components.yaml"
    documents = list(yaml.safe_load_all(urllib.request.urlopen(url, timeout=30).read()))
    for document in documents:
        if document.get("kind") == "Deployment":
            args = document["spec"]["template"]["spec"]["containers"][0]["args"]
            for flag in ("--kubelet-insecure-tls", "--kubelet-preferred-address-types=InternalIP"):
                if flag not in args:
                    args.append(flag)
    target = Path(config["remote_root"]) / "private/metrics-server.yaml"
    target.write_text(yaml.safe_dump_all(documents))
    subprocess.run(["kubectl", "apply", "-f", str(target)], check=True, timeout=90)
    subprocess.run(
        [
            "kubectl",
            "-n",
            "kube-system",
            "rollout",
            "status",
            "deployment/metrics-server",
            "--timeout=180s",
        ],
        check=True,
        timeout=190,
    )


def network_check(config):
    namespace = "holmes-setup-check"
    existing = subprocess.run(
        ["kubectl", "get", "namespace", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if existing.returncode == 0:
        raise RuntimeError(f"{namespace} exists; inspect and preserve it before retrying setup")
    kubectl("create", "namespace", namespace)
    try:
        for i in range(3):
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": f"probe-{i}", "namespace": namespace},
                "spec": {
                    "nodeName": f"node{i}",
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "probe",
                            "image": config["versions"]["probe_image"],
                            "command": [
                                "sh",
                                "-c",
                                "printf ok > /tmp/index.html; httpd -f -p 8080 -h /tmp",
                            ],
                            "resources": {
                                "requests": {"cpu": "5m", "memory": "8Mi"},
                                "limits": {"cpu": "100m", "memory": "32Mi"},
                            },
                        }
                    ],
                },
            }
            subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=json.dumps(pod),
                text=True,
                check=True,
                timeout=30,
            )
        subprocess.run(
            [
                "kubectl",
                "wait",
                "-n",
                namespace,
                "--for=condition=Ready",
                "pod",
                "--all",
                "--timeout=120s",
            ],
            check=True,
            timeout=130,
        )
        pods = json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json"))["items"]
        for source in pods:
            for target in pods:
                if source["metadata"]["name"] == target["metadata"]["name"]:
                    continue
                result = kubectl(
                    "exec",
                    "-n",
                    namespace,
                    source["metadata"]["name"],
                    "--",
                    "wget",
                    "-q",
                    "-T",
                    "5",
                    "-O",
                    "-",
                    f"http://{target['status']['podIP']}:8080/",
                )
                if result != "ok":
                    raise RuntimeError("Cross-node pod HTTP check failed")
            kubectl(
                "exec",
                "-n",
                namespace,
                source["metadata"]["name"],
                "--",
                "nslookup",
                "kubernetes.default.svc.cluster.local",
            )
        print("All six cross-node pod HTTP paths and three DNS lookups passed", flush=True)
    finally:
        subprocess.run(
            ["kubectl", "delete", "namespace", namespace, "--timeout=120s", "--wait=true"],
            check=True,
            timeout=130,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--network-check", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.prepare:
        prepare_metrics(config)
    deadline = time.monotonic() + 240
    while True:
        hosts = sample_all(config)
        errors = cluster_errors(config) + recovery_errors(hosts)
        if not errors:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("; ".join(errors))
        print("Waiting for recovery: " + "; ".join(errors), flush=True)
        time.sleep(10)
    if args.network_check:
        network_check(config)
        hosts = sample_all(config)
        errors = cluster_errors(config) + recovery_errors(hosts)
        if errors:
            raise RuntimeError("Post-probe recovery failed: " + "; ".join(errors))
    report = {"time": time.time(), "hosts": hosts, "errors": errors}
    (Path(config["remote_root"]) / "private/setup-health.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print("Three-node API/etcd/DNS/metrics/Calico/services/headroom checks passed", flush=True)


if __name__ == "__main__":
    main()
