from __future__ import annotations

from typing import Any

from dischat.discourse.models import DiscourseEvent


def normalize_post_event(post_payload: dict[str, Any]) -> DiscourseEvent:
    mentioned = tuple(user["username"] for user in post_payload.get("mentioned_users", []))
    target_username = None
    if post_payload.get("reply_to_user"):
        target_username = post_payload["reply_to_user"].get("username")
    event_type = "bridged_thread_reply" if post_payload.get("reply_to_post_number") else "new_topic"
    if target_username:
        event_type = "direct_reply"
    elif mentioned:
        event_type = "mention"
        target_username = mentioned[0]
    return DiscourseEvent(
        discourse_topic_id=post_payload["topic_id"],
        discourse_post_id=post_payload["id"],
        reply_to_post_number=post_payload.get("reply_to_post_number"),
        event_type=event_type,
        category_id=post_payload.get("category_id"),
        author_username=post_payload["username"],
        target_discourse_username=target_username,
        raw_payload_json=post_payload,
    )
