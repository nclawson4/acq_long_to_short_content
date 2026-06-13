"""Workers — the AI agents that the harness governs.

A worker is a callable behind a single Protocol (agents.base.Worker). The
harness instantiates whichever worker it's configured to drive and hands it
an Inbox; the worker drives a multi-turn loop calling tools, reading
checkpoint feedback, and returning an Outbox.

Spec requirement (hackathon "Should"):
    "Swappable agent interface — dropping in a different agent requires
     no changes to the harness."

Bonus:
    "A second worker is swapped in during the demo to prove portability."

Current implementations:
    agents.claude_worker.ClaudeWorker   — primary; Claude-driven loop
    agents.swap_worker.SwapWorker       — second worker for the bonus demo

Both conform to the same Protocol; the harness picks via env var WORKER.
"""
from .base import Worker  # noqa: F401

__all__ = ["Worker"]
