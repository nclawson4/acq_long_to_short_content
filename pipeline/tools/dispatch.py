"""Tool dispatch choke point.

Implements the pattern from the harness deck (slide 14):

    def dispatch(call):
        if call.name not in TOOLS:        # allow-list
            return err("unknown tool")
        log.info("tool", name=call.name)  # observe
        try:
            out = TOOLS[call.name].fn(**call.args)
        except Exception as e:
            out = f"tool error: {e}"      # fail as data
        return out

Adapted for this build: we don't return errors as model-readable strings
(no LLM is choosing tools), but we still:
    1. allow-list at the registry boundary,
    2. wrap every dispatch in a span tagged with the tool name + clip id,
    3. catch + record exceptions so the orchestrator decides retry vs drop
       rather than the tool's call site.
"""
from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ..observability.tracer import get_tracer
from .registry import get_tool


def dispatch(tool_name: str, /, *args: Any, _span_attrs: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """Look up `tool_name` on the allow-list and call it inside a span.

    Re-raises the tool's exception so the orchestrator's retry policy applies.
    """
    fn = get_tool(tool_name)
    tracer = get_tracer()
    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        for k, v in (_span_attrs or {}).items():
            if v is not None:
                span.set_attribute(k, v)
        try:
            result = fn(*args, **kwargs)
            span.set_attribute("status", "ok")
            return result
        except Exception as e:
            span.set_attribute("status", "error")
            span.set_attribute("error.class", type(e).__name__)
            span.set_attribute("error.message", str(e)[:500])
            span.record_exception(e)
            raise
