from __future__ import annotations

from typing import Any


def get_reply_parent_event_id(event: dict[str, Any]) -> str | None:
    return event.get("content", {}).get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
