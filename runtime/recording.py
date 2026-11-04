"""Fail-soft native persistence plus the one required request-batch observer.

Native call() owns execution. Neither hook changes SDK objects or call arguments.
An agent request is identified by the pinned call_stream completion signature;
compaction uses a different signature. Unknown signatures stay unresolved.
"""
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path


def snapshot(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: snapshot(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(v) for v in value]
    return value


class Recorder:
    def __init__(self, directory):
        self.root = Path(directory)
        self.failed = False
        self.sequence = 0
        self.events = {}

    def save(self, relative, value, append=False):
        try:
            payload = json.dumps(snapshot(value), ensure_ascii=False, allow_nan=False)
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
            with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                # fsync at stage/result boundaries, not around every parallel tool.
                if not append:
                    os.fsync(handle.fileno())
            return True
        except Exception as exc:
            self.failed = True
            logging.warning("Incomplete HolmesGPT capture (%s): %s", relative, type(exc).__name__)
            return False

    def overlay(self, kind, stage=None, **fields):
        self.sequence += 1
        self.save("overlay/records.jsonl", {
            "origin": "integration_overlay", "sequence": self.sequence,
            "kind": kind, "stage": stage,
            "recorded_at": datetime.now(timezone.utc).isoformat(), **fields,
        }, append=True)

    @contextmanager
    def observe(self, ai, stage, messages, configuration):
        self.overlay("stage_start", stage, messages=messages, configuration=configuration)
        completion, stream = ai.llm.completion, ai.call_stream
        count = 0

        @wraps(completion)
        def observed_completion(*args, **kwargs):
            response = completion(*args, **kwargs)
            # Native agent loop passes both keys; native compaction passes neither.
            purpose = "agent" if "response_format" in kwargs and kwargs.get("stream") is False else "compaction_or_other"
            try:
                choices = [{"index": getattr(c, "index", None),
                            "tool_calls": snapshot(getattr(c.message, "tool_calls", None))}
                           for c in response.choices]
                self.overlay("completion", stage, observation_point="llm.completion:return_before_merge",
                             purpose=purpose, choices=choices, response_id=getattr(response, "id", None),
                             response_model=getattr(response, "model", None))
            except Exception as exc:
                self.failed = True
                logging.warning("Incomplete completion capture: %s", type(exc).__name__)
            return response

        @wraps(stream)
        def observed_stream(*args, **kwargs):
            nonlocal count
            for event in stream(*args, **kwargs):
                count += 1
                self.save(f"native/{stage}/events.jsonl", event, append=True)
                # Receipt ordering, not native process/tool start timestamps.
                self.overlay("event_received", stage, native_line=count,
                             event=getattr(event.event, "value", event.event))
                yield event

        ai.llm.completion, ai.call_stream = observed_completion, observed_stream
        started = time.monotonic()
        try:
            yield
        finally:
            ai.llm.completion, ai.call_stream = completion, stream
            self.events[stage] = count
            self.overlay("stage_capture_closed", stage, native_events=count,
                         capture_failed=self.failed, elapsed_wall_seconds=time.monotonic() - started)

    def flush(self, paths):
        for relative in paths:
            try:
                with (self.root / relative).open("rb") as handle:
                    os.fsync(handle.fileno())
            except Exception as exc:
                self.failed = True
                logging.warning("Incomplete capture flush (%s): %s", relative, type(exc).__name__)

    def result(self, stage, result):
        self.flush([f"native/{stage}/events.jsonl"])
        self.save(f"native/{stage}/result.json", result)
        self.overlay("stage_result", stage, capture_failed=self.failed)

    def finish(self):
        self.flush([f"native/{s}/events.jsonl" for s in self.events])
        self.overlay("capture_end", capture_failed=self.failed, events=self.events)
        self.flush(["overlay/records.jsonl"])
        if self.failed:
            self.overlay("capture_incomplete", capture_failed=True)
