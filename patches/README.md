# Integration changes

The submodules select exact public source commits. Deployment keeps those checkouts unchanged and applies these patches to separate runtime/build copies.

| File | Purpose and scope |
| --- | --- |
| [shell-timeout.patch](holmesgpt/shell-timeout.patch) | Fix Bash timeouts that leave pipeline children running: kill the shell’s process group and bound output collection. Detached descendants can survive. |
| [holmes-registration.patch](sregym/holmes-registration.patch) | Register the adapter with SREGym’s container launcher. |
| [holmes-provider.patch](sregym/holmes-provider.patch) | Allow the selected model-provider endpoint under SREGym’s filtered network policy. |
| [sonnet-sampling.patch](sregym/sonnet-sampling.patch) | Omit judge sampling parameters rejected by Sonnet 5.5. Keep the rubric and scoring unchanged. |
| [case-output-directory.patch](sregym/case-output-directory.patch) | Give each case a separate output directory. |
| [worker-ssh-key.patch](sregym/worker-ssh-key.patch) | Use a dedicated key and verified host keys for native worker fault commands. |
| [stop-traffic-before-reference-cleanup.patch](sregym/stop-traffic-before-reference-cleanup.patch) | Stop owned traffic before restoring the fault, after evaluation has closed. |
| [agent.py](../runtime/agent.py) | Set stage prompts, CORE/CLI tools, Bash permissions, MCP connections and model/step settings; retain conversation and result files between stages. HolmesGPT owns the agent loop. |
| [recording.py](../runtime/recording.py) | Save native events/results and exact tool-request batches. File I/O adds overhead; capture failures are reported without changing execution. |
| [campaign.py](../runtime/campaign.py), [guardian.py](../runtime/guardian.py) | Supervise cases, mark interventions unscored and require verified cleanup/recovery before continuing. |
| [convert.py](../runtime/convert.py) | Convert saved records offline to `trajectory.json`, retaining source references and missing-data markers. |

The SDK is built from source for the timeout correction. Its package version remains upstream’s `0.0.0`; the recorded commit and patch identify the build. Host/judge and agent dependencies use separate locks in [images/](../images/). Tool and image-build versions are in [versions.toml](../versions.toml).
