from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MatrixMessageEvent:
    event_id: str
    room_id: str
    sender: str
    body: str
    is_dm: bool
    parent_event_id: str | None = None
