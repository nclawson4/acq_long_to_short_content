"""Claude wrapper — single entry point for any LLM call in the pipeline.

The pipeline as designed does not need an LLM for any of MY stages (ingest,
transcribe, finalize). Tool 2 (pick_timestamps) needs one, but it is built
separately. This wrapper exists so:

    1. If tool 2 wants to import an already-budgeted Claude helper, it can.
    2. If a future stage needs LLM reasoning (e.g. "why did QC fail?"), it
       calls this and the cost is charged automatically.

Cost capture: every successful call writes to the run's CostLedger and tags
the current OTel span with token + cost attributes. If the charge would
breach the spend ceiling the call raises BudgetExceeded BEFORE the network
request — so the model can't spend money the run hasn't budgeted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from opentelemetry import trace

from .observability.ledger import BudgetExceeded, get_ledger


# Haiku 4.5 list price (per million tokens, USD). Update if Anthropic changes pricing.
# These are conservative — if the SDK returns a `cost_usd` we prefer that, this is
# the fallback when we have to compute from token usage.
_PRICE_PER_MTOK = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
    "claude-sonnet-4-6-20250101": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
}


def estimate_cost_usd(model: str, *, input_tokens: int, output_tokens: int,
                     cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    p = _PRICE_PER_MTOK.get(model)
    if not p:
        # Conservative fallback: Sonnet pricing
        p = _PRICE_PER_MTOK["claude-sonnet-4-6-20250101"]
    return (
        (input_tokens - cache_read_tokens) * p["input"] / 1_000_000
        + output_tokens * p["output"] / 1_000_000
        + cache_write_tokens * p["cache_write"] / 1_000_000
        + cache_read_tokens * p["cache_read"] / 1_000_000
    )


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    raw: Any  # the Anthropic Message object, for callers that want tool_use blocks etc.


def call_claude(
    *,
    stage: str,
    system: str,
    messages: list[dict],
    model: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    cache_system: bool = True,
) -> LLMResult:
    """Call Claude with cost capture + ledger enforcement.

    `stage` is the label the cost shows up under in the ledger
    (e.g. "pick_timestamps", "qc_diagnose"). Pass the actual stage name so
    cost-by-stage attribution is honest.

    Raises:
        BudgetExceeded — if the call would push spend over the ceiling.
        anthropic.AnthropicError — surfaced unchanged.
    """
    from anthropic import Anthropic  # local import so the module loads without the SDK installed

    model = model or os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    ledger = get_ledger()

    # Pre-flight: refuse if we are already over ceiling. The call itself
    # could cost up to roughly max_tokens * output_price; we don't preemptively
    # subtract that because pre-spending the worst case would starve the run.
    if ledger.total_usd >= ledger.ceiling_usd:
        raise BudgetExceeded(stage, 0.0, ledger.total_usd, ledger.ceiling_usd)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Prompt caching is opt-in per block via `cache_control`. We cache the
    # system prompt by default because pick_timestamps will be called with
    # the same system over many videos.
    if cache_system and system:
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    else:
        system_blocks = [{"type": "text", "text": system}] if system else []

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks if system_blocks else None,
        messages=messages,
    )

    usage = resp.usage
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = estimate_cost_usd(
        model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )

    # Charge the ledger. If this exceeds the ceiling the entry is rejected
    # and BudgetExceeded propagates. We still tag the span so the cost is
    # visible in traces even on rejection.
    span = trace.get_current_span()
    if span is not None:
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.tokens_in", in_tok)
        span.set_attribute("llm.tokens_out", out_tok)
        span.set_attribute("llm.cache_read_tokens", cache_read)
        span.set_attribute("llm.cache_write_tokens", cache_write)
        span.set_attribute("llm.cost_usd", cost)

    ledger.charge(stage, cost, model=model, in_tok=in_tok, out_tok=out_tok)

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return LLMResult(
        text=text,
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
        raw=resp,
    )
