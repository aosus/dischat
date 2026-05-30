from __future__ import annotations

import html


def _htmlify_text(body: str) -> str:
    paragraphs = [segment.strip() for segment in body.split("\n\n")]
    rendered: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        escaped = html.escape(paragraph).replace("\n", "<br>")
        if escaped.startswith("# "):
            rendered.append(f"<h1>{escaped[2:]}</h1>")
        else:
            rendered.append(f"<p>{escaped}</p>")
    return "".join(rendered) or f"<p>{html.escape(body)}</p>"


def plain_text(body: str) -> dict[str, str]:
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": _htmlify_text(body),
    }


def plain_notice(body: str) -> dict[str, str]:
    return {"msgtype": "m.notice", "body": body}


def reply_message(body: str, *, parent_event_id: str) -> dict[str, object]:
    return {
        **plain_text(body),
        "m.relates_to": {
            "m.in_reply_to": {
                "event_id": parent_event_id,
            }
        },
    }
