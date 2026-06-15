"""Alarm webhook notifier — pushes ERROR + CRITICAL alarms to an external
incident channel so on-call sees them without watching logs.

Wired into AlarmBus.fire(); a no-op when ACQ_ALARM_WEBHOOK_URL is unset.
Posts a compact JSON payload that's compatible with Slack/Discord-style
incoming webhooks AND with PagerDuty Events API v2 (auto-detected).

We never raise from this module — a webhook hiccup must not break the
pipeline. Failures are best-effort logged.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import Alarm


_WEBHOOK_URL = os.environ.get("ACQ_ALARM_WEBHOOK_URL", "").strip()
_MIN_SEVERITY = os.environ.get("ACQ_ALARM_MIN_SEVERITY", "error").strip().lower()
_SERVICE_NAME = os.environ.get("ACQ_ALARM_SERVICE", "acq-clipper").strip()

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _meets_threshold(severity_value: str) -> bool:
    return _SEVERITY_RANK.get(severity_value, 0) >= _SEVERITY_RANK.get(_MIN_SEVERITY, 2)


def _build_slack_payload(alarm: "Alarm") -> dict:
    sev = alarm.severity.value.upper()
    emoji = {"INFO": ":information_source:", "WARNING": ":warning:",
             "ERROR": ":rotating_light:", "CRITICAL": ":fire:"}.get(sev, ":bell:")
    stage = alarm.stage or "harness"
    text = f"{emoji} *{sev}* | `{_SERVICE_NAME}` | `{stage}` | {alarm.name}"
    if alarm.message:
        text += f"\n> {alarm.message[:300]}"
    if alarm.context:
        ctx_lines = [f"`{k}`: {str(v)[:120]}" for k, v in list(alarm.context.items())[:6]]
        text += "\n" + " · ".join(ctx_lines)
    text += f"\n_recommended action: `{alarm.recommended_action.value}`_"
    return {"text": text}


def _looks_like_pagerduty(url: str) -> bool:
    return "events.pagerduty.com" in url or "events.eu.pagerduty.com" in url


def _build_pagerduty_payload(alarm: "Alarm") -> dict:
    sev_map = {"info": "info", "warning": "warning", "error": "error", "critical": "critical"}
    return {
        "routing_key": os.environ.get("ACQ_PAGERDUTY_ROUTING_KEY", ""),
        "event_action": "trigger",
        "payload": {
            "summary": f"[{_SERVICE_NAME}] {alarm.name}: {alarm.message or ''}"[:1024],
            "severity": sev_map.get(alarm.severity.value, "error"),
            "source": _SERVICE_NAME,
            "component": alarm.stage or "harness",
            "custom_details": {
                **alarm.context,
                "recommended_action": alarm.recommended_action.value,
                "clip_id": alarm.clip_id,
                "fired_at": alarm.fired_at,
            },
        },
    }


def _post(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # Most webhooks return 200/204; surface non-2xx as a log line.
        try:
            print(f"[alarm-notifier] HTTP {e.code} from {url}: {e.read()[:200].decode(errors='replace')}")
        except Exception:
            print(f"[alarm-notifier] HTTP {e.code} from {url}")
    except Exception as e:
        print(f"[alarm-notifier] failed: {type(e).__name__}: {e}")


def notify_async(alarm: "Alarm") -> None:
    """Fire-and-forget. Returns immediately; HTTP runs on a daemon thread."""
    if not _WEBHOOK_URL:
        return
    if not _meets_threshold(alarm.severity.value):
        return
    if _looks_like_pagerduty(_WEBHOOK_URL):
        payload = _build_pagerduty_payload(alarm)
    else:
        payload = _build_slack_payload(alarm)
    t = threading.Thread(target=_post, args=(_WEBHOOK_URL, payload), daemon=True)
    t.start()


__all__ = ["notify_async"]
