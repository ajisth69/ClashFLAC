"""Short-lived in-memory progress snapshots for active download requests."""
from __future__ import annotations

import time
from typing import Any

_jobs: dict[str, dict[str, Any]] = {}


def update(job_id: str | None, stage: str, progress: int, *, error: str | None = None) -> None:
    if not job_id:
        return
    now = time.time()
    _jobs[job_id] = {
        "stage": stage,
        "progress": max(0, min(100, int(progress))),
        "error": error,
        "updated_at": now,
    }
    # Keep completed/error entries briefly so the browser can receive the final state.
    expired = [key for key, value in _jobs.items() if now - value["updated_at"] > 300]
    for key in expired:
        _jobs.pop(key, None)


def get(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)
