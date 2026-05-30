from __future__ import annotations


def excerpt_text(body: str, limit: int = 280) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"
