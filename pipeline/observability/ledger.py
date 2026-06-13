"""Cost ledger — the spend-ceiling enforcement mechanism.

Architecture defense: "The spend ceiling is the mechanism that guarantees
the sub-$1 claim. Not a hope, an enforced limit."

How it works:
    Every billable operation (Deepgram call, Claude call, ffmpeg-CPU-time
    estimate, Blob egress) calls `ledger.charge(stage, usd, detail=...)`.
    `charge` raises `BudgetExceeded` synchronously if the new total would
    exceed `cost_ceiling_usd`. The orchestrator catches that and marks the
    job `budget_exceeded` — no further work runs.

Why a per-instance object vs a global: makes the ledger pickleable per job,
gives a clean test seam, and avoids cross-job leakage on warm function
instances (Fluid Compute reuses instances across concurrent requests).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional


class BudgetExceeded(Exception):
    """Raised when a charge would push total above the ceiling.

    Catch this at the orchestrator level. The current stage is rolled back,
    the job is marked budget_exceeded, and we surface the per-stage spend
    breakdown so the operator can see what overran.
    """

    def __init__(self, stage: str, attempted_usd: float, total_usd: float, ceiling: float) -> None:
        super().__init__(
            f"budget exceeded at stage={stage}: "
            f"attempted +${attempted_usd:.4f}, total ${total_usd:.4f}, ceiling ${ceiling:.2f}"
        )
        self.stage = stage
        self.attempted_usd = attempted_usd
        self.total_usd = total_usd
        self.ceiling = ceiling


@dataclass
class CostEntry:
    stage: str
    usd: float
    detail: dict = field(default_factory=dict)


class CostLedger:
    def __init__(self, ceiling_usd: float, target_usd: float = 0.20) -> None:
        self._ceiling = ceiling_usd
        self._target = target_usd
        self._entries: list[CostEntry] = []
        self._lock = threading.Lock()

    def charge(self, stage: str, usd: float, **detail) -> CostEntry:
        """Record a cost and enforce the ceiling.

        Raises BudgetExceeded if the new total would exceed the ceiling.
        The entry is NOT recorded when the check fails, so the ledger always
        reflects what was actually accepted.
        """
        with self._lock:
            current = sum(e.usd for e in self._entries)
            new_total = current + usd
            if new_total > self._ceiling:
                raise BudgetExceeded(stage, usd, new_total, self._ceiling)
            entry = CostEntry(stage=stage, usd=usd, detail=dict(detail))
            self._entries.append(entry)
            return entry

    @property
    def total_usd(self) -> float:
        with self._lock:
            return sum(e.usd for e in self._entries)

    @property
    def ceiling_usd(self) -> float:
        return self._ceiling

    @property
    def target_usd(self) -> float:
        return self._target

    @property
    def over_target(self) -> bool:
        return self.total_usd > self._target

    def by_stage(self) -> dict[str, float]:
        out: dict[str, float] = {}
        with self._lock:
            for e in self._entries:
                out[e.stage] = out.get(e.stage, 0.0) + e.usd
        return out

    def entries(self) -> list[dict]:
        with self._lock:
            return [
                {"stage": e.stage, "usd": e.usd, "detail": e.detail}
                for e in self._entries
            ]

    def remaining_usd(self) -> float:
        return max(0.0, self._ceiling - self.total_usd)


_current: Optional[CostLedger] = None


def get_ledger() -> CostLedger:
    """Per-process current ledger.

    The orchestrator sets this at the start of each job and clears it at the
    end. Stages and tools read this singleton so they don't need the ledger
    passed through their signatures.
    """
    global _current
    if _current is None:
        # Soft default for tests / accidental direct stage runs.
        _current = CostLedger(ceiling_usd=1.00, target_usd=0.20)
    return _current


def set_ledger(ledger: CostLedger) -> None:
    global _current
    _current = ledger


def reset_ledger() -> None:
    global _current
    _current = None
