"""OpenTelemetry setup + helpers.

The OTLP HTTP exporter is enabled iff OTEL_EXPORTER_OTLP_ENDPOINT is set —
otherwise spans only land in the per-job JSON trace (exporters.py). That
keeps local dev and the smoke test dependency-free while letting prod ship
spans to Langfuse / SigNoz / Honeycomb / whatever.
"""
from __future__ import annotations

import functools
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False
_recorder: Optional["_InMemoryRecorder"] = None


def init_tracing(service_name: str = "acq-clipper") -> None:
    """Idempotent. Called once at module import time from the API entry."""
    global _initialized, _recorder
    if _initialized:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            headers = {}
            raw_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            if raw_headers:
                for kv in raw_headers.split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        headers[k.strip()] = v.strip()
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception as e:
            # Tracing is observability — it must never break the pipeline.
            print(f"[tracer] OTLP exporter init failed, continuing without: {e}")

    # Always also attach the in-memory recorder so the JSON trace exporter
    # has something to read from. It implements SpanProcessor directly.
    _recorder = _InMemoryRecorder()
    provider.add_span_processor(_recorder)

    trace.set_tracer_provider(provider)
    _initialized = True


def get_tracer() -> trace.Tracer:
    if not _initialized:
        init_tracing()
    return trace.get_tracer("acq-clipper")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    """Convenience: context-manager span with attributes pre-set."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


def traced(name: str) -> Callable:
    """Decorator: wrap a function in a span named after the stage.

    Captures duration, exception, and any attributes the function sets via
    `span.set_attribute(...)` inside its body (it reads the current span from
    the tracer context).
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tracer = get_tracer()
            with tracer.start_as_current_span(name) as s:
                t0 = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                    s.set_attribute("status", "ok")
                    return result
                except Exception as e:
                    s.set_attribute("status", "error")
                    s.set_attribute("error.class", type(e).__name__)
                    s.set_attribute("error.message", str(e)[:500])
                    s.record_exception(e)
                    raise
                finally:
                    s.set_attribute("duration_ms", int((time.perf_counter() - t0) * 1000))
        return wrapper
    return deco


# ---------------- in-memory recorder ----------------

class _InMemoryRecorder(SpanProcessor):
    """OTel SpanProcessor that buffers finished spans for the JSON exporter.

    OTel ships exporters but they are async / batched / network-bound. We want
    a sync, in-process record so the per-job JSON trace can be dumped on the
    same function invocation that produced it.

    Inherits from SpanProcessor so newer SDK private hooks (e.g. _on_ending)
    are inherited as no-ops.
    """
    def __init__(self) -> None:
        super().__init__()
        self.finished: list[dict[str, Any]] = []

    def on_start(self, span, parent_context=None):  # noqa: D401
        pass

    def on_end(self, span) -> None:
        try:
            ctx = span.get_span_context()
            self.finished.append({
                "name": span.name,
                "span_id": format(ctx.span_id, "016x"),
                "trace_id": format(ctx.trace_id, "032x"),
                "parent_span_id": (
                    format(span.parent.span_id, "016x") if span.parent else None
                ),
                "start_ns": span.start_time,
                "end_ns": span.end_time,
                "duration_ms": int((span.end_time - span.start_time) / 1_000_000)
                    if span.start_time and span.end_time else None,
                "attributes": dict(span.attributes or {}),
                "status": span.status.status_code.name if span.status else "UNSET",
            })
        except Exception:
            pass  # never let observability crash the pipeline

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def get_in_memory_spans() -> list[dict[str, Any]]:
    """Read the in-memory recorder's buffer.

    Used by exporters.dump_run_trace() and by the smoke test.
    """
    if _recorder is None:
        return []
    return list(_recorder.finished)


def clear_in_memory_spans() -> None:
    """Drop buffered spans — used between test cases."""
    if _recorder is not None:
        _recorder.finished.clear()
