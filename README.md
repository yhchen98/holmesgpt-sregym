# HolmesGPT on SREGym

Run the benchmark on three CloudLab nodes and collect agent trajectories. Run the commands below on your laptop.

## 1. Prepare access

- Use three fresh **Ubuntu 22.04 x86-64** nodes on the same private LAN: `node0` is the control node; `node1` and `node2` are workers.
- Confirm SSH access and `sudo -n true` work on each node. Trust their SSH host keys before setup.
- Setup requires **16 GiB available RAM and 30 GiB free disk per node**. The tested nodes had about 64 GiB RAM each; larger workloads may exceed the setup minimum.
- Have an **Anthropic API key** ready. Setup accepts hidden input, a local key file, `ANTHROPIC_API_KEY`, or `~/.sregym.anthropic_api_key` on node0.
- Install **Git, OpenSSH and Python 3.12.15** on your laptop.

## 2. Install

Replace `<repository-url>` with this repository’s Git URL:

```sh
git clone --recurse-submodules <repository-url> holmesgpt-sregym
cd holmesgpt-sregym
python3.12 -m venv .venv
.venv/bin/python -m pip install -r images/controller.lock
.venv/bin/python deploy.py --ansible .venv/bin/ansible-playbook
```

Follow the prompts for node aliases, private IPs and credentials. If your LAN differs from `10.10.1.0/24`, add `--private-cidr YOUR_SUBNET` to the deployment command.

Setup installs Kubernetes and the benchmark, builds the agent image and checks all three nodes. It may reboot nodes to select the required kernel. Allow 30–90 minutes. Skip the optional example to proceed directly to the full run.

## 3. Run and check progress

```sh
.venv/bin/python run.py --all
.venv/bin/python status.py --run run-01
```

Use the run name printed by the launcher in place of `run-01`. The benchmark runs in tmux on node0 and continues after you disconnect. The launcher prints the command to attach to it.

Defaults: **Sonnet 5.5 for agent and judge, full profile, one attempt per case, 100 agent iterations per stage and a shared 30-minute agent deadline per case**. The pinned setup selects 217 cases. Model calls incur API charges.

To continue a stopped campaign:

```sh
.venv/bin/python run.py --resume run-01
```

Resume skips closed cases. It halts if an interrupted case or cluster recovery needs inspection; logs are under `~/holmesgpt-sregym/private/runs/run-01/` on node0. Do not redeploy during a run. To start a separate repeated campaign, use `run.py --all` again.

## 4. Collect results

After the campaign stops:

```sh
.venv/bin/python collect.py --run run-01
```

Results and logs are saved locally in `private/collected/run-01/`; converted trajectories are in its `normalized/` directory.

To also export graded, fully captured trajectories for sharing:

```sh
.venv/bin/python collect.py --run run-01 --share
```

Exports go to `trajectories/run-01/<problem>/trajectory.json`, alongside a results CSV and run summary. Review their contents before publishing.
