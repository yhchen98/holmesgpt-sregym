# Runtime guide

[Open the diagram](diagrams/runtime-overview/runtime-overview.html). Click a component for its inputs, outputs, behavior and source links; expand a tool to read its definition.

## A case, from setup to evaluation

A **problem** is a test case: an application, a fault and evaluation rules.

1. SREGym deploys the application and injects the fault.
2. Our [stage adapter](../runtime/agent.py) reads the application details and stage, then configures HolmesGPT.
3. HolmesGPT diagnoses the cause and, when enabled, attempts mitigation. Diagnosis is read-only; mitigation permits selected Kubernetes writes.
4. The adapter submits HolmesGPT’s answer unchanged. SREGym evaluates it, advances stages and restores the environment.

The adapter supplies the task, scope, permission guidance, model settings and CORE/CLI tool selection. HolmesGPT’s prompt builder supplies its tool instructions. Between stages, the adapter replaces system instructions and retains other conversation messages and the shared result directory. Each stage allows 100 iterations within SREGym’s shared 30-minute agent deadline.

## The model/tool loop

1. `ToolCallingLLM` prepares the conversation, compacting older context when needed.
2. `DefaultLLM` sends it through LiteLLM to the configured model.
3. The model returns an answer or a batch of tool names and arguments.
4. `ToolExecutor` dispatches the batch. Tools can finish in a different order; calls to the same MCP server are serialized.
5. `ToolCallResult` formats outputs/errors as tool messages. HolmesGPT adds them to the conversation and calls the model again.

These are software layers around the configured model. A batch can contain several calls, so 100 iterations does not mean 100 tool calls. On the final iteration, HolmesGPT requests an answer without tools. [Native loop][loop] · [Result formatting][results].

## Tools and destinations

The reference configuration exposes these 19 tools. Toolsets group tools; classes implement them. The diagram groups implementations into YAML, Bash, Python and MCP routes.

<!-- tool-inventory:start -->

| Tool | Toolset | Execution route | Returned information |
| --- | --- | --- | --- |
| `kubernetes_jq_query` | `kubernetes/core` | YAMLTool script → kubectl discovery/paginated reads → local jq | Selected JSON, scope, counts and errors |
| `kubernetes_tabular_query` | `kubernetes/core` | YAMLTool script → kubectl custom columns → local row filtering | Table rows, headers, count and errors |
| `kubernetes_count` | `kubernetes/core` | YAMLTool script → paginated kubectl reads → local jq count | Match count, preview, scope and errors |
| `kubectl_top_pods` | `kubernetes/live-metrics` | YAMLTool command → kubectl top pods -A → Kubernetes Metrics API | Current pod CPU/memory usage |
| `kubectl_top_nodes` | `kubernetes/live-metrics` | YAMLTool command → kubectl top nodes → Kubernetes Metrics API | Current node CPU/memory usage |
| `get_prometheus_target` | `kubernetes/kube-prometheus-stack` | YAMLTool script → kubectl raw GET → Kubernetes service proxy → Prometheus /api/v1/targets → local jq | Target details or access/error information |
| `bash` | `bash` | RunBashCommand → permission checks → Bash; kubectl uses the Kubernetes access proxy | Command output/status or rejection/timeout information |
| `fetch_pod_logs` | `kubernetes/logs` | PodLoggingTool → KubernetesLogsToolset → parallel kubectl logs → local filtering | Filtered container log text and errors |
| `TodoWrite` | `core_investigation` | TodoWriteTool → format the supplied task list in the agent conversation | Task descriptions and statuses |
| `fetch_webpage` | `internet` | FetchWebpage → validated HTTP GET → HTML-to-Markdown conversion | Page text/Markdown or refusal/failure information |
| `tcp_check` | `connectivity_check` | TcpCheckTool → destination/budget checks → Python socket connection | Connection success or failure information |
| `read_image_file` | `bash` | ReadImageFile → read an image in HolmesGPT result storage | Image data and file information |
| `fetch_skill` | `skills` | SkillsFetcher → configured catalog/files → skill content | Troubleshooting instructions or lookup errors |
| `get_metrics` | `sregym_prometheus` | RemoteMCPTool → SREGym Python handler → Prometheus GET /api/v1/query; Prometheus evaluates PromQL | Visibility-filtered metric data as text |
| `get_alerts` | `sregym_prometheus` | RemoteMCPTool → SREGym Python handler → Prometheus GET /api/v1/alerts | Visible firing alerts as text |
| `get_services` | `sregym_jaeger` | RemoteMCPTool → SREGym Python handler → Jaeger GET /api/services | Application service names as text |
| `get_operations` | `sregym_jaeger` | RemoteMCPTool → SREGym Python handler → Jaeger GET /api/operations with a service name | Operation names as text |
| `get_traces` | `sregym_jaeger` | RemoteMCPTool → SREGym Python handler → Jaeger GET /api/traces with service/time filters | Application request traces as text |
| `get_dependency_graph` | `sregym_jaeger` | RemoteMCPTool → SREGym Python handler → Jaeger GET /api/dependencies with a time range | Service dependencies as text |

<!-- tool-inventory:end -->

- **Kubernetes:** kubectl requests pass through SREGym’s access proxy. jq and table filtering run locally in the agent container. `kubectl top` uses the Kubernetes Metrics API; PromQL runs in Prometheus.
- **MCP:** HolmesGPT’s MCP client calls SREGym’s Python handlers, which send HTTP requests to Prometheus or Jaeger. Jaeger stores application request traces, separate from agent trajectories and pod logs.
- **Access:** filtered networking permits the selected model provider and local SREGym services. Arbitrary websites and direct outbound TCP probes are blocked. SDK permissions and SREGym filtering can reject a listed tool’s request; filtered mode also blocks Kubernetes service-proxy target lookup.
- **Counts:** one tool call can perform several downstream requests through pagination, retries or parallel log retrieval. Recorded tool calls are not a network capture.

## Integration and supervision

[Integration changes](../patches/README.md) lists the patches and behavior settings, including Sonnet sampling compatibility and Bash timeout handling.

The [guardian](../runtime/guardian.py) monitors host resources and responsiveness. Intentional API/service failures are observations during a case. Dangerous resource pressure triggers native interruption; interventions remain unscored. After grading and cleanup, the supervisor stops remaining helpers in that case’s process group and checks recovery before advancing. Uncertain cleanup halts the campaign. SSH-based monitoring cannot guarantee containment after host failure.

To use another agent, replace the HolmesGPT adapter/image and recording/export code. SREGym still supplies case setup, stage APIs, Kubernetes filtering, MCP services and evaluation.

## Saved records

| Files | Contents |
| --- | --- |
| `native/<stage>/events.jsonl`, `result.json` | Serialized HolmesGPT events and returned conversation/results |
| `overlay/records.jsonl` | Exact requested tool arguments/IDs, batches, stage configuration, submission receipts and capture status |
| `trajectory.json` | Offline conversion to ATIF, the shared trajectory format, with source references and explicit gaps |

The [recorder](../runtime/recording.py) persists information during execution; the [converter](../runtime/convert.py) runs after collection. Recording adds file I/O overhead. A capture failure is reported without changing agent execution.

Converted observations use client-facing result data. The model-facing messages can also contain effective parameters, metadata, error formatting and images. Large outputs may become stored files; compaction can remove older context. Use the native conversation to inspect the model-facing messages. SDK token/cost defaults do not establish measured billing.

## Build and edit

Git submodule commits select the sources; [versions.toml](../versions.toml) and [images/](../images/) pin tools, dependencies and the agent-image recipe. SREGym’s workload and MCP images retain upstream tags, some mutable. The diagram follows the pinned source; the exact source revision of the earlier deployed `sregym-mcp:latest` image is unknown.

The HTML opens without a server. To edit it, change [runtime.json](diagrams/runtime-overview/runtime.json) and rebuild with Python 3 and Graphviz `dot` (tested with 16.1.0):

```sh
git submodule update --init --recursive
python3 docs/diagrams/runtime-overview/render.py
```

| JSON fields | Edit |
| --- | --- |
| `title`, `nodes[].title`, `lines`, `purpose` | Headline, component labels, optional notes and purpose |
| `nodes[].input`, `output`, `behavior` | Card bullets |
| `nodes[].sources`, `tool_definitions`, `tool_index` | Source links, tool inventory and definition excerpts |
| `edges[].label`, `label_offset` | Link text and placement |
| `rows`, `nodes[].column`, `owners` | Layout, alignment and ownership colors |

Keep node IDs stable: links and rows use them. The renderer embeds definitions from pinned source objects and updates this guide’s tool table from the same JSON.

[loop]: https://github.com/HolmesGPT/holmesgpt/blob/21ca948b8b4935a2ed0aa98dae05449d10953005/holmes/core/tool_calling_llm.py#L1220-L1595
[results]: https://github.com/HolmesGPT/holmesgpt/blob/21ca948b8b4935a2ed0aa98dae05449d10953005/holmes/core/models.py#L11-L107
