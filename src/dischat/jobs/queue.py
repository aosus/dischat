from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(slots=True)
class DeliveryJob:
    id: int
    event_id: int
    target_type: str
    target_mxid: str | None
    matrix_room_id: str | None
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime = datetime.now(UTC)
    last_error: str | None = None


def schedule_retry(job: DeliveryJob, *, error: str, delay: timedelta) -> DeliveryJob:
    job.attempts += 1
    job.status = "failed"
    job.last_error = error
    job.next_attempt_at = datetime.now(UTC) + delay
    return job
