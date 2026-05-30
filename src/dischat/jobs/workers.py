from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class WorkerResult:
    complete: bool
    error: str | None = None
