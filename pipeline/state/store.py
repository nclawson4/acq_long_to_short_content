"""State store backed by Upstash Redis REST API.

Why Upstash over local memory: Vercel function FS is ephemeral and instances
can be reused or evicted at any time. A run that pauses for retry or polling
needs durable state outside the function process.

Why REST not the TCP redis protocol: Upstash REST works from Vercel Functions
without TCP egress concerns, and `httpx` is already in our dependency set.

Keys:
    job:{job_id}              JSON-serialized JobState
    job:{job_id}:trace        JSON list of span records (observability/exporters.py)
    job:{job_id}:ledger       JSON list of cost entries

A single 7-day TTL is set on every key so completed runs are auto-evicted.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

import httpx

from ..types import JobState

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class _LocalDictStore:
    """In-process fallback when no Upstash creds are configured.

    Useful for the smoke test and local dev. NOT durable across processes.
    """
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def set(self, key: str, value: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def lpush(self, key: str, value: str) -> None:
        existing = self._data.get(key)
        items: list[str] = json.loads(existing) if existing else []
        items.append(value)
        self._data[key] = json.dumps(items)

    def lrange(self, key: str) -> list[str]:
        existing = self._data.get(key)
        return json.loads(existing) if existing else []


class _UpstashRestStore:
    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.Client(timeout=10.0)

    def _call(self, *parts: str) -> Any:
        # Upstash REST: POST with command as JSON array OR GET with path
        # We use POST + JSON to avoid URL-encoding pitfalls for large values.
        resp = self._client.post(self._url, headers=self._headers, json=list(parts))
        resp.raise_for_status()
        return resp.json().get("result")

    def get(self, key: str) -> Optional[str]:
        return self._call("GET", key)

    def set(self, key: str, value: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._call("SET", key, value, "EX", str(ttl_seconds))

    def delete(self, key: str) -> None:
        self._call("DEL", key)

    def lpush(self, key: str, value: str) -> None:
        self._call("RPUSH", key, value)
        self._call("EXPIRE", key, str(DEFAULT_TTL_SECONDS))

    def lrange(self, key: str) -> list[str]:
        result = self._call("LRANGE", key, "0", "-1")
        return list(result) if result else []


class StateStore:
    """Public interface over either backend. All values JSON in/out."""

    def __init__(self, backend: Any) -> None:
        self._b = backend

    # ----- job state -----

    def save_job(self, state: JobState) -> None:
        state.updated_at = datetime.utcnow()
        self._b.set(f"job:{state.job_id}", state.model_dump_json())

    def load_job(self, job_id: str) -> Optional[JobState]:
        raw = self._b.get(f"job:{job_id}")
        if raw is None:
            return None
        return JobState.model_validate_json(raw)

    # ----- trace + ledger append-only logs -----

    def append_span(self, job_id: str, span: dict[str, Any]) -> None:
        self._b.lpush(f"job:{job_id}:trace", json.dumps(span))

    def read_trace(self, job_id: str) -> list[dict[str, Any]]:
        return [json.loads(s) for s in self._b.lrange(f"job:{job_id}:trace")]

    def append_cost(self, job_id: str, entry: dict[str, Any]) -> None:
        self._b.lpush(f"job:{job_id}:ledger", json.dumps(entry))

    def read_ledger(self, job_id: str) -> list[dict[str, Any]]:
        return [json.loads(s) for s in self._b.lrange(f"job:{job_id}:ledger")]


_singleton: Optional[StateStore] = None


def get_store() -> StateStore:
    """Singleton state store. Falls back to in-process dict if no Upstash creds."""
    global _singleton
    if _singleton is not None:
        return _singleton
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if url and token:
        _singleton = StateStore(_UpstashRestStore(url, token))
    else:
        _singleton = StateStore(_LocalDictStore())
    return _singleton


def reset_store_for_tests() -> None:
    """Drop the singleton — only the smoke test calls this."""
    global _singleton
    _singleton = None
