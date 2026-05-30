from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class AuditEntry:
    action: str
    discourse_username_used: str
    success: bool
    mxid: str | None = None
    platform: str | None = None
    discourse_user_id_used: int | None = None
    topic_id: int | None = None
    post_id: int | None = None
    matrix_room_id: str | None = None
    matrix_event_id: str | None = None
    error_message: str | None = None
