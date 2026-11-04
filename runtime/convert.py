"""Offline native/overlay -> ATIF conversion. Source records are never rewritten."""
import argparse
import json
import subprocess
from pathlib import Path


def jsonl(path, gaps):
    if not path.exists():
        gaps.append(f"missing:{path.name}")
        return []
    records = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            records.append((number, value))
        except (ValueError, UnicodeError):
            gaps.append(f"invalid_line:{path.name}:{number}")
    return records


def convert(directory):
    directory = Path(directory)
    source_root = Path(__file__).resolve().parents[1]
    try:
        command = ["git", "-C", str(source_root)]
        converter_revision = subprocess.check_output([*command, "log", "-1", "--format=%H", "--", "runtime/convert.py"], text=True, stderr=subprocess.DEVNULL).strip() or None
        converter_modified = bool(subprocess.check_output([*command, "status", "--porcelain", "--", "runtime/convert.py"], stderr=subprocess.DEVNULL))
    except (OSError, subprocess.SubprocessError):
        converter_revision, converter_modified = None, None
    gaps, steps, orphans, stats = [], [], [], {}
    records = jsonl(directory / "overlay/records.jsonl", gaps)
    stages = {}; received = {}; requests = {}; starts = {}; definitions = {}; ended = set(); submitted = set()
    begin = next((r for _, r in records if r.get("kind") == "capture_start"), {})
    expected = begin.get("stages", [])
    if not begin.get("session_id"):
        gaps.append("missing_session_identity")
    if not expected:
        gaps.append("missing_configured_stages")
    def add(source, message, stage, ref, **data):
        step = {"step_id": len(steps)+1, "source": source, "message": message,
                "extra": {"stage": stage, "source_ref": ref}, **data}
        steps.append(step)
        return step
    for line, record in records:
        stage = record.get("stage"); kind = record.get("kind")
        ref = f"overlay/records.jsonl:{line}"
        if record.get("capture_failed"):
            gaps.append(f"recorder_failure:{line}")
        if kind == "stage_start":
            config = record.get("configuration", {})
            stages[stage] = dict(jsonl(directory / "native" / stage / "events.jsonl", gaps))
            definitions[stage] = config.get("tools")
            received[stage] = set()
            starts[stage] = len(steps)+1
            add("user", config.get("prompt", ""), stage, ref, timestamp=record.get("recorded_at"))
        elif kind == "completion" and record.get("purpose") == "agent":
            calls = []
            for choice in record.get("choices", []):
                for call in choice.get("tool_calls") or []:
                    function = call.get("function", {}); raw = function.get("arguments")
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except ValueError:
                        args = None
                    if not call.get("id") or not function.get("name"):
                        orphans.append({"source_ref": ref, "request": call})
                        gaps.append(f"unrepresentable_request:{line}")
                        continue
                    extra = {"raw_arguments": raw, "choice_index": choice.get("index"), "source_ref": ref}
                    if not isinstance(args, dict):
                        orphans.append({"source_ref": ref, "request": call, "reason": "arguments_not_an_object"})
                        gaps.append(f"unrepresentable_arguments:{stage}:{call['id']}")
                        continue
                    calls.append({"tool_call_id": call["id"], "function_name": function["name"],
                                  "arguments": args, "extra": extra})
            if calls:
                step = add("agent", "", stage, ref, tool_calls=calls, timestamp=record.get("recorded_at"))
                for call in calls:
                    requests.setdefault((stage, call["tool_call_id"]), []).append(step)
        elif kind == "event_received":
            number = record.get("native_line")
            event = stages.get(stage, {}).get(number)
            if not event:
                gaps.append(f"missing_native_event:{stage}:{number}")
                continue
            received[stage].add(number)
            native_ref = f"native/{stage}/events.jsonl:{number}"
            data = event.get("data", {}); name = event.get("event")
            if name == "tool_calling_result":
                call_id = data.get("tool_call_id"); candidates = requests.get((stage, call_id), [])
                if candidates:
                    step = candidates[-1]
                    result = data.get("result") or {}
                    content = result.get("data")
                    # Client-facing native result, not a reconstructed model-facing string.
                    if content is not None and not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False)
                    observation = {"source_call_id": call_id, "content": content,
                                   "extra": {"source_ref": native_ref, "native_client_result": data,
                                             "completion_receipt_time": record.get("recorded_at"),
                                             **{k: result.get(k) for k in ("status", "error", "return_code", "elapsed_seconds")}}}
                    step.setdefault("observation", {"results": []})["results"].append(observation)
                else:
                    orphans.append({"source_ref": native_ref, "native_event": event})
                    gaps.append(f"orphan_tool_result:{stage}:{number}")
            elif name == "ai_message":
                if steps and steps[-1].get("tool_calls") and not steps[-1].get("observation") and steps[-1]["extra"]["stage"] == stage:
                    step = steps[-1]
                else:
                    step = add("system", "", stage, native_ref)
                step["message"] = data.get("content") or ""
                step["reasoning_content"] = data.get("reasoning")
                step["extra"]["native_message_ref"] = native_ref
            elif name == "ai_answer_end":
                add("agent", data.get("content") or "", stage, native_ref, timestamp=record.get("recorded_at"))
                ended.add(stage)
            elif name == "start_tool_calling":
                if (stage, data.get("id")) not in requests:
                    gaps.append(f"uncaptured_request:{stage}:{number}")
            # All native events, including approvals/compaction/errors, remain addressable below.
        elif kind == "submission":
            receipt = record.get("receipt", {})
            if receipt.get("status") == "200" and receipt.get("message") == "Submission received" and receipt.get("stage") == stage:
                submitted.add(stage)
            else:
                gaps.append(f"unconfirmed_submission:{stage}:{line}")
    for stage in expected:
        path = directory / "native" / stage / "result.json"
        if path.exists():
            try:
                result = json.loads(path.read_text())
                stats[stage] = {k: result.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens", "total_cost", "num_compactions", "num_llm_calls", "finish_reason")}
            except ValueError:
                gaps.append(f"invalid_native_result:{stage}")
        else:
            gaps.append(f"missing_native_result:{stage}")
        if stage not in ended or stage not in submitted:
            gaps.append(f"unfinished_stage:{stage}")
        if received.get(stage, set()) != set(stages.get(stage, {})):
            gaps.append(f"native_event_receipt_gap:{stage}")
    if not records or records[-1][1].get("kind") != "capture_end":
        gaps.append("missing_capture_end")
    sequence = [r.get("sequence") for _, r in records]
    if sequence != list(range(1, len(sequence)+1)):
        gaps.append("overlay_sequence_gap")
    for (stage, call_id), batches in requests.items():
        if not any(o.get("source_call_id") == call_id for s in batches for o in s.get("observation", {}).get("results", [])):
            gaps.append(f"missing_tool_result:{stage}:{call_id}")
    ranges = [{"stage": stage, "start_step": start,
               "end_step": max((x["step_id"] for x in steps if x["extra"]["stage"] == stage), default=start),
               "status": "completed" if stage in ended and stage in stats else "incomplete",
               "submitted": stage in submitted} for stage, start in starts.items()]
    # A global tool definition is safe only when identical in all observed stages.
    by_name = {}
    for stage_definitions in definitions.values():
        for definition in stage_definitions or []:
            by_name.setdefault(definition.get("function", {}).get("name"), []).append(definition)
    common_definitions = [values[0] for values in by_name.values()
                          if len(values) == len(definitions) and all(v == values[0] for v in values)]
    return {"schema_version": "ATIF-v1.7", "session_id": begin.get("session_id"),
        "agent": {"name": "holmesgpt", "version": begin.get("sdk_package_version", "unknown"), "model_name": begin.get("model"), "tool_definitions": common_definitions},
        "steps": steps, "extra": {"holmesgpt": {"stages": ranges, "configured_stages": expected, "capture_complete": not gaps, "capture_issues": sorted(set(gaps)), "source": "native_plus_labeled_overlay"}, "capture_complete": not gaps, "capture_gaps": sorted(set(gaps)),
        "artifact_role": "derived_from_native_and_overlay", "session_id_origin": "SREGym opaque artifact identity supplied to adapter", "converter": "runtime.convert", "converter_revision": converter_revision, "converter_working_tree_modified": converter_modified, "integration_revision": begin.get("integration_revision"), "tool_definitions_by_stage": definitions,

        "native_stage_statistics": stats, "statistics_status": "SDK defaults retained; measurement availability not inferred",
        "native_events": {s: [{"source_ref": f"native/{s}/events.jsonl:{n}", "event": e.get("event")} for n,e in es.items()] for s,es in stages.items()},
        "unmatched_records": orphans, "timestamp_source": "overlay receipt time; not native tool start time"}}


def export(directory, output):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    if directory / "native" in output.parents or directory / "overlay" in output.parents:
        raise ValueError("Conversion cannot overwrite source evidence")
    data = convert(directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = export(args.case, args.output)
    print(f"{len(data['steps'])} steps; capture_complete={data['extra']['capture_complete']}")
