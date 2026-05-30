from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class DiscoursePost:
    post_id: int
    topic_id: int
    post_number: int
    category_id: int | None
    username: str
    raw: str
    reply_to_post_number: int | None = None
    target_username: str | None = None
    mentioned_usernames: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class DiscourseEvent:
    discourse_topic_id: int
    discourse_post_id: int
    reply_to_post_number: int | None
    event_type: str
    category_id: int | None
    author_username: str
    target_discourse_username: str | None
    raw_payload_json: dict[str, Any]
