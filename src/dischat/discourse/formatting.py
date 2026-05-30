from __future__ import annotations


def excerpt_text(body: str, limit: int = 280) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def format_topic_delivery(*, title: str, body: str) -> str:
    cleaned_title = title.strip()
    cleaned_body = body.strip()
    if not cleaned_title:
        return cleaned_body
    if not cleaned_body:
        return f"# {cleaned_title}"
    return f"# {cleaned_title}\n\n{cleaned_body}"
