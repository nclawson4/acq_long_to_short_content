"""Hard limits — turn cap, wall-clock, retry cap.

The cost ceiling is enforced inside observability.ledger because it's coupled
to cost accounting. Everything else lives here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


class TurnCapExceeded(Exception):
    """Orchestrator has looped more times than configured."""


class WallClockExceeded(Exception):
    """Orchestrator has been running longer than configured."""


@dataclass
class HardLimits:
    """One object the orchestrator consults at the top of every loop iteration."""
    max_turns: int
    max_wall_seconds: int
    max_retries_per_stage: int
    started_at: float  # time.monotonic() when the run began

    @classmethod
    def start(cls, *, max_turns: int, max_wall_seconds: int, max_retries_per_stage: int) -> "HardLimits":
        return cls(
            max_turns=max_turns,
            max_wall_seconds=max_wall_seconds,
            max_retries_per_stage=max_retries_per_stage,
            started_at=time.monotonic(),
        )

    def check(self, turn: int) -> None:
        """Raise if either limit is breached. Called at top of each loop iter."""
        if turn >= self.max_turns:
            raise TurnCapExceeded(f"turn {turn} exceeds cap {self.max_turns}")
        if time.monotonic() - self.started_at > self.max_wall_seconds:
            raise WallClockExceeded(
                f"wall-clock {self.elapsed_seconds():.1f}s exceeds {self.max_wall_seconds}s"
            )

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_wall_seconds - self.elapsed_seconds())
