"""Small outer safety monitor. Native main.py alone owns grading and recovery."""

import subprocess
import time
from runtime.health import kubectl, sample_all

GIB = 1024**3
INTERVAL = 10
# Native interrupt joins evaluation (300s) plus the largest frozen problem's
# cleanup (1800s). Allow another 300s for launcher/API/process shutdown.
STOP_BUDGET = 2400


class Guardian:
    def __init__(self, config):
        self.config = config
        self.previous = {}
        self.misses = {}

    def observe(self):
        samples = sample_all(self.config)
        reasons = []
        for host in samples:
            node = host["node"]
            missing = host.get("error") or host.get("latency", 0) > 20
            self.misses[node] = self.misses.get(node, 0) + 1 if missing else 0
            if missing:
                if self.misses[node] >= 3:
                    reasons.append(
                        f"{node}: three missing/stale observations; supervision lost"
                    )
                continue
            available = host["memory_available"]
            old = self.previous.get(node)
            growth = 0
            if old:
                elapsed = host["received_monotonic"] - old["received_monotonic"]
                growth = max(
                    0, (old["memory_available"] - available) / max(elapsed, 0.001)
                )
            host["memory_loss_bytes_per_second"] = growth
            self.previous[node] = host
            if available < 4 * GIB:
                reasons.append(f"{node}: less than 4 GiB RAM reserve")
            if available < 8 * GIB and growth * 45 > available - 4 * GIB:
                reasons.append(
                    f"{node}: RAM growth threatens the reserve within 45 seconds"
                )
            if (
                available < 8 * GIB
                and host["pressure"]["memory"].get("full", {}).get("avg10", 0) > 10
            ):
                reasons.append(f"{node}: low RAM with sustained memory stalls")
            for path, disk in host["disks"].items():
                if (
                    disk["available"] < 5 * GIB
                    or disk["free_inodes"] < 0.02 * disk["total_inodes"]
                ):
                    reasons.append(f"{node}:{path}: emergency disk/inode reserve")
        # API, CPU/I/O pressure, services and node state are diagnostic during
        # intentional faults. They become strict checks only between cases.
        try:
            api = kubectl("get", "--raw=/readyz", timeout=3)
        except (OSError, subprocess.SubprocessError):
            api = "unavailable"
        return {
            "samples": samples,
            "api": api,
            "reasons": reasons,
            "monotonic": time.monotonic(),
        }
