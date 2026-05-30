from __future__ import annotations

from dataclasses import dataclass

from dischat.discourse.formatting import excerpt_text
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
    body = str(event.raw_payload_json.get("raw", ""))
    if job.target_type == "room" and job.matrix_room_id is not None:
        room_link = await room_links.get_by_room_id(job.matrix_room_id)
        rendered_body = (
            body if room_link is not None and room_link.full_content else excerpt_text(body)
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
            )
        else:
            result = await matrix_client.send_notice(job.matrix_room_id, rendered_body)
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
        result = await matrix_client.send_dm(
            job.target_mxid,
            body or translate("pairing.unpaired", locale),
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
