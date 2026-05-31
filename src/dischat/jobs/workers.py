from __future__ import annotations

import re
from dataclasses import dataclass

from dischat.discourse.formatting import (
    excerpt_text,
    format_plain_html,
    format_topic_delivery,
    format_topic_delivery_html,
)
from dischat.i18n import translate
from dischat.matrix.client import MatrixClient
from dischat.storage.repositories import (
    ChatAccountRepository,
    DeliveryJobRecord,
    DeliveryMessageRepository,
    DiscourseEventRepository,
    RoomLinkRepository,
)


@dataclass(slots=True, frozen=True)
class WorkerResult:
    complete: bool
    error: str | None = None


def _render_discourse_body(payload: dict[str, object]) -> str:
    raw = payload.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    cooked = payload.get("cooked")
    if not isinstance(cooked, str) or not cooked.strip():
        return ""
    # Topic reads on this Discourse instance expose cooked HTML but may omit raw.
    text = re.sub(r"<br\s*/?>", "\n", cooked)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    return text.strip()


def _render_discourse_html(payload: dict[str, object]) -> str:
    cooked = payload.get("cooked")
    if isinstance(cooked, str) and cooked.strip():
        return cooked.strip()
    return format_plain_html(_render_discourse_body(payload))


def _render_delivery_content(
    payload: dict[str, object], *, full_content: bool
) -> tuple[str, dict[str, str] | None]:
    body = _render_discourse_body(payload)
    body_html = _render_discourse_html(payload)
    title = payload.get("topic_title")
    if payload.get("reply_to_post_number") is None and isinstance(title, str) and title.strip():
        topic_body = body if full_content else excerpt_text(body)
        topic_body_html = body_html if full_content else format_plain_html(topic_body)
        rendered_body = format_topic_delivery(title=title, body=topic_body)
        rendered_html = format_topic_delivery_html(
            title=title,
            body_html=topic_body_html,
            excerpt=not full_content,
        )
        return rendered_body, {"format": "org.matrix.custom.html", "formatted_body": rendered_html}
    rendered_body = body if full_content else excerpt_text(body)
    rendered_html = body_html if full_content else format_plain_html(rendered_body)
    return rendered_body, {"format": "org.matrix.custom.html", "formatted_body": rendered_html}


async def deliver_job(
    *,
    job: DeliveryJobRecord,
    discourse_events: DiscourseEventRepository,
    delivery_messages: DeliveryMessageRepository,
    chat_accounts: ChatAccountRepository,
    room_links: RoomLinkRepository,
    matrix_client: MatrixClient,
) -> WorkerResult:
    event = await discourse_events.get_by_id(job.event_id)
    if event is None:
        return WorkerResult(complete=False, error="missing_discourse_event")
    if job.target_type == "room" and job.matrix_room_id is not None:
        room_link = await room_links.get_by_room_id(job.matrix_room_id)
        rendered_body, formatted = _render_delivery_content(
            event.raw_payload_json,
            full_content=room_link is not None and room_link.full_content,
        )
        parent_mapping = None
        reply_to_post_id = event.raw_payload_json.get("reply_to_discourse_post_id")
        if isinstance(reply_to_post_id, int):
            parent_mapping = await delivery_messages.get_by_discourse_post_and_room(
                discourse_post_id=reply_to_post_id,
                matrix_room_id=job.matrix_room_id,
            )
        if parent_mapping is not None:
            result = await matrix_client.send_reply(
                job.matrix_room_id,
                rendered_body,
                parent_mapping.matrix_event_id,
                formatted=formatted,
            )
        else:
            result = await matrix_client.send_text(
                job.matrix_room_id,
                rendered_body,
                formatted=formatted,
            )
        await delivery_messages.create_mapping(
            discourse_topic_id=event.discourse_topic_id,
            discourse_post_id=event.discourse_post_id,
            matrix_room_id=job.matrix_room_id,
            matrix_event_id=result.event_id,
            target_type="room",
            target_mxid=None,
            parent_delivery_message_id=parent_mapping.id if parent_mapping is not None else None,
        )
        return WorkerResult(complete=True)
    if job.target_type == "dm" and job.target_mxid is not None:
        account = await chat_accounts.get_by_mxid(job.target_mxid)
        locale = account.response_locale if account is not None else "en"
        body, formatted = _render_delivery_content(event.raw_payload_json, full_content=True)
        result = await matrix_client.send_dm(
            job.target_mxid,
            body or translate("pairing.unpaired", locale),
            formatted=formatted,
        )
        if result.room_id is None:
            return WorkerResult(complete=False, error="missing_dm_room_id")
        await delivery_messages.create_mapping(
            discourse_topic_id=event.discourse_topic_id,
            discourse_post_id=event.discourse_post_id,
            matrix_room_id=result.room_id,
            matrix_event_id=result.event_id,
            target_type="dm",
            target_mxid=job.target_mxid,
            parent_delivery_message_id=None,
        )
        return WorkerResult(complete=True)
    return WorkerResult(complete=False, error="unsupported_delivery_target")
