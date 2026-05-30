from __future__ import annotations


def plain_notice(body: str) -> dict[str, str]:
    return {"msgtype": "m.notice", "body": body}


def reply_message(body: str, *, parent_event_id: str) -> dict[str, object]:
    return {
        "msgtype": "m.text",
        "body": body,
        "m.relates_to": {
            "m.in_reply_to": {
                "event_id": parent_event_id,
            }
        },
    }
