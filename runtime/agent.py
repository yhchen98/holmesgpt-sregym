"""SREGym protocol adapter; HolmesGPT's native call() owns agent execution."""
import os
# SDK reads this once, on import. Sonnet 5.5 rejects sampling parameters.
if "claude-sonnet-5-5" in os.environ.get("AGENT_MODEL_ID", "").lower():
    os.environ["TEMPERATURE"] = "none"

import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import requests
import yaml
from holmes.config import Config
from holmes.core.prompt import build_initial_ask_messages
from holmes.core.tools import ToolsetTag
from holmes.core.tools_utils.filesystem_result_storage import tool_result_storage
from runtime.recording import Recorder

WRITE_PREFIXES = ["kubectl patch", "kubectl scale", "kubectl rollout restart", "kubectl rollout undo",
                  "kubectl set", "kubectl apply", "kubectl annotate", "kubectl label", "kubectl delete",
                  "kubectl create configmap", "kubectl create service"]
SECRET_PREFIXES = ["kubectl get secret", "kubectl get secrets", "kubectl describe secret", "kubectl describe secrets"]
HOST = os.environ.get("API_HOSTNAME", "localhost")
if HOST in {"0.0.0.0", "::"}:
    HOST = "127.0.0.1"
API = f"http://{HOST}:{os.environ.get('API_PORT', '8000')}"
MCP = f"http://{HOST}:{os.environ.get('MCP_SERVER_PORT', '9954')}"


def request(method, route, **kwargs):
    response = requests.request(method, API + route, timeout=kwargs.pop("timeout", 15), **kwargs)
    response.raise_for_status()
    return response.json()


def wait_stage(allowed):
    # SREGym owns the shared attempt deadline and stops the agent container.
    while True:
        try:
            stage = request("GET", "/status").get("stage")
            if stage in allowed or stage == "done":
                return stage
        except requests.RequestException:
            pass
        time.sleep(1)


def configure(stage, scratch, storage):
    policy = {"builtin_allowlist": "core", "allow": WRITE_PREFIXES if stage == "mitigation" else [],
              "deny": SECRET_PREFIXES + (WRITE_PREFIXES if stage == "diagnosis" else [])}
    settings = {"toolsets": {"bash": {"enabled": True, "config": policy}}, "mcp_servers": {
        f"sregym_{name}": {"description": description, "config": {"url": f"{MCP}/{name}/sse", "mode": "sse"},
                           "llm_instructions": instruction}
        for name, description, instruction in [
            ("prometheus", "Prometheus metrics exposed by SREGym", "Query application metrics when useful for fault investigation."),
            ("jaeger", "Jaeger traces exposed by SREGym", "Use application distributed traces when useful for fault investigation.")]
    }}
    path = scratch / f"{stage}.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    cfg = Config.load_from_file(config_file=path, api_key=os.environ["ANTHROPIC_API_KEY"],
                                model=os.environ["AGENT_MODEL_ID"], max_steps=100)
    # Same CORE/CLI tags as native ask. Explicit MCP configuration supplies observability.
    ai = cfg.create_toolcalling_llm(toolset_tag_filter=[ToolsetTag.CORE, ToolsetTag.CLI],
                                    enable_all_toolsets_possible=True, tool_results_dir=storage)
    if "bash" not in ai.tool_executor.tools_by_name:
        raise RuntimeError("Native bash toolset is unavailable")
    return cfg, ai, settings


def stage_messages(prompt, cfg, ai, previous):
    initial = build_initial_ask_messages(initial_user_prompt=prompt, file_paths=None,
        tool_executor=ai.tool_executor, skills=cfg.get_skill_catalog(), system_prompt_additions=(
        "SREGym stage permissions: diagnosis is read-only. During mitigation, the configured Kubernetes write "
        "prefixes extend the bash toolset's read-only default; all other approval and denial rules continue to apply."))
    if previous is None:
        return initial
    return ([m for m in initial if m["role"] == "system"] +
            [m for m in previous if m["role"] != "system"] + [m for m in initial if m["role"] != "system"])


def main():
    os.umask(0o077)
    recorder = Recorder(Path("/logs") if Path("/logs").exists() else Path("logs"))
    try:
        stage = wait_stage({"diagnosis", "mitigation"})
        if stage == "done":
            return
        app = request("GET", "/get_app")
        stages = app.get("stages") or [stage]
        recorder.overlay("capture_start", stages=stages, model=os.environ["AGENT_MODEL_ID"],
            sdk_package_version=version("holmesgpt"), integration_revision=os.environ.get("HOLMES_SREGYM_REVISION"), session_id=os.environ.get("SREGYM_ARTIFACT_ID"))
        scope = ", ".join(app.get("namespaces") or [app["namespace"]])
        context = (f"Application: {app['app_name']}\nRelevant namespace(s): {scope}\n"
                   f"Application description: {app['descriptions']}\n"
                   "Investigate only the affected application/namespace(s) and direct dependencies. Do not inspect "
                   "benchmark internals, faults, hidden namespaces, or grading resources.\n")
        previous = None
        # Native result storage lives across stage handoff, separate from evidence files.
        with tempfile.TemporaryDirectory(prefix="holmes-config-") as tmp, tool_result_storage() as storage:
            for expected in stages:
                if expected not in {"diagnosis", "mitigation"}:
                    raise ValueError(f"Unsupported stage {expected}")
                stage = wait_stage({expected})
                if stage == "done":
                    return
                cfg, ai, settings = configure(stage, Path(tmp), storage)
                instruction = ("Diagnose the current SRE incident using live evidence; explain the root cause precisely. "
                               "Do not mutate Kubernetes or application state.\n") if stage == "diagnosis" else (
                               "Now MITIGATE this SRE incident. Apply permitted Kubernetes changes within the scoped "
                               "environment, then check whether they worked. Use only SREGym's proxied Kubernetes "
                               "access and observability tools. Do not change unrelated resources. Summarize actual changes.\n")
                prompt = instruction + context
                messages = stage_messages(prompt, cfg, ai, previous)
                configuration = {"prompt": prompt, "model": os.environ["AGENT_MODEL_ID"], "max_steps": 100,
                    "toolset_tags": ["CORE", "CLI"], "settings": settings,
                    "tools": ai.tool_executor.get_all_tools_openai_format(), "approval_callback": None,
                    "tool_results_dir": str(storage), "context_handoff": previous is not None}
                print(f"[holmes] starting {stage}; {len(ai.tool_executor.tools_by_name)} native tools", flush=True)
                with recorder.observe(ai, stage, messages, configuration):
                    result = ai.call(messages)
                recorder.result(stage, result)
                if not result.result:
                    raise RuntimeError(f"Native SDK returned an empty {stage} answer")
                previous = result.messages
                receipt = request("POST", "/submit", json={"stage": stage, "solution": result.result}, timeout=310)
                recorder.overlay("submission", stage, receipt=receipt)
                print(f"[holmes] submitted {stage}", flush=True)
    finally:
        recorder.finish()


if __name__ == "__main__":
    main()
