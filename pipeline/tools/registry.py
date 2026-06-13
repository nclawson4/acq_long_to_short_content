"""Tool allow-list (architecture pillar 3, action guardrail).

A tool that isn't registered cannot be dispatched. Registration is explicit
and happens at boot from the orchestrator — there is no auto-discovery.
That's deliberate: when tool 2 ships, the only place a developer adds it is
here, and that change is what gives the orchestrator permission to call it.
"""
from __future__ import annotations

from typing import Any, Callable

from .adapters import StubAddCaptions, StubCropVideo, StubPickTimestamps


class ToolNotAllowed(Exception):
    """Dispatch was called with a tool name that isn't on the allow-list."""


_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_tool(name: str, fn: Callable[..., Any], *, replace: bool = False) -> None:
    if not replace and name in _REGISTRY:
        raise ValueError(f"tool {name!r} already registered; pass replace=True to override")
    _REGISTRY[name] = fn


def get_tool(name: str) -> Callable[..., Any]:
    if name not in _REGISTRY:
        raise ToolNotAllowed(f"tool not on allow-list: {name!r} (registered: {list(_REGISTRY)})")
    return _REGISTRY[name]


def list_tools() -> list[str]:
    return sorted(_REGISTRY)


def install_default_stubs() -> None:
    """Wire up stub implementations for tools 1/2/3.

    Call this at startup BEFORE real implementations are registered. The
    real registrations override (replace=True). Doing it this way means the
    pipeline boots in a usable state even if a tool hasn't shipped yet.
    """
    register_tool("pick_timestamps", StubPickTimestamps(), replace=True)
    register_tool("crop_video", StubCropVideo(), replace=True)
    register_tool("add_captions", StubAddCaptions(), replace=True)


def reset_registry_for_tests() -> None:
    _REGISTRY.clear()
