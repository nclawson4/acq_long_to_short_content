"""Worker Protocol — the single interface every agent conforms to.

Why a Protocol and not an ABC: workers can be implemented anywhere and don't
need to import this module to be valid — Protocol is structural typing.
That makes "drop in a different worker" mean literally dropping in a module
that exports a class with this shape.

A Worker:
    - Takes an Inbox (job material) and the live Harness it runs inside
    - Drives an agent loop using harness-provided tools, guardrails, and
      checkpoints
    - Returns an Outbox describing what happened

Workers do not call exit(), raise on tool failures, or write to disk
directly. Disk is the harness's job; tool failures arrive as ToolCallResult
with ok=False.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from harness.material import Inbox, Outbox
    from harness.runtime import Harness   # defined in #25


@runtime_checkable
class Worker(Protocol):
    """Structural type for all workers."""
    name: str        # short id, e.g. "claude-haiku-orchestrator"
    version: str     # semver-ish

    def run(self, harness: "Harness", inbox: "Inbox") -> "Outbox":
        ...
