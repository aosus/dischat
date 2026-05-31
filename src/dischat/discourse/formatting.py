from __future__ import annotations

import html


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


def format_plain_html(body: str) -> str:
    paragraphs = [segment.strip() for segment in body.split("\n\n")]
    rendered: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        rendered.append(f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>")
    return "".join(rendered) or f"<p>{html.escape(body)}</p>"


def format_topic_delivery_html(*, title: str, body_html: str, excerpt: bool) -> str:
    cleaned_title = html.escape(title.strip())
    cleaned_body = body_html.strip()
    if excerpt:
        if cleaned_body:
            return f"<p><strong>{cleaned_title}</strong></p>{cleaned_body}"
        return f"<p><strong>{cleaned_title}</strong></p>"
    if not cleaned_title:
        return cleaned_body
    return f"<h1>{cleaned_title}</h1>{cleaned_body}"
