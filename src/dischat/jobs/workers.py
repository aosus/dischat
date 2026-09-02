from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from dischat.discourse.formatting import (
    excerpt_text,
    format_plain_html,
    format_topic_delivery,
    format_topic_delivery_html,
)
from dischat.i18n import translate
from dischat.security.audit import (
    ACTION_DM_DELIVERY,
    ACTION_ROOM_DELIVERY,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    AuditEntry,
    failure_reason,
    record_audit_attempt,
    record_audit_entry,
    update_audit_outcome,
)
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryJobRecord,
    DeliveryMessageRecord,
    RoomLinkRecord,
    TargetType,
)

logger = logging.getLogger(__name__)
SYSTEM_ACTOR = "system"


@dataclass(slots=True, frozen=True)
class WorkerResult:
    complete: bool
    error: str | None = None


class StoredEvent(Protocol):
    discourse_topic_id: int
    discourse_post_id: int
    raw_payload_json: dict[str, Any]


class DiscourseEventsRepo(Protocol):
    async def get_by_id(self, event_id: int) -> StoredEvent | None: ...


class DeliveryMessagesRepo(Protocol):
    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None: ...
    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]: ...

    async def create_mapping(
        self,
        *,
        discourse_topic_id: int,
        discourse_post_id: int,
        matrix_room_id: str,
        matrix_event_id: str,
        target_type: TargetType,
        target_mxid: str | None,
        parent_delivery_message_id: int | None,
    ) -> DeliveryMessageRecord: ...


class ChatAccountsRepo(Protocol):
    async def get_by_mxid(self, mxid: str) -> ChatAccount | None: ...


class RoomLinksRepo(Protocol):
    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None: ...


class DeliveryJobsRepo(Protocol):
    async def ensure_matrix_tx_id(self, job_id: int) -> str: ...
    async def ensure_matrix_device_id(self, job_id: int, *, device_id: str) -> str: ...
    async def get_matrix_dm_room_id(self, job_id: int) -> str | None: ...
    async def pin_matrix_dm_room(self, job_id: int, *, room_id: str) -> str: ...


class AuditLogsRepo(Protocol):
    async def record(self, entry: AuditEntry) -> int | None: ...

    async def update_outcome(
        self,
        audit_log_id: int,
        *,
        success: bool,
        error_message: str | None,
        post_id: int | None = None,
        matrix_event_id: str | None = None,
        matrix_room_id: str | None = None,
    ) -> None: ...


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


async def _find_existing_room_mapping(
    *,
    delivery_messages: Any,
    discourse_post_id: int,
    matrix_room_id: str,
) -> DeliveryMessageRecord | None:
    """Return an existing mapping for (post, room) if this delivery already happened.

    Used for at-least-once reconciliation: when a job is retried after a crash
    or lease expiry, the previous attempt's Matrix send may have succeeded even
    though its result was never persisted. In that case a live mapping exists
    and re-sending would post the message twice; the caller skips the send.
    """
    return await delivery_messages.get_by_discourse_post_and_room(
        discourse_post_id=discourse_post_id,
        matrix_room_id=matrix_room_id,
    )


async def _find_existing_dm_mapping(
    *,
    delivery_messages: Any,
    discourse_post_id: int,
    target_mxid: str,
) -> DeliveryMessageRecord | None:
    """Return a prior DM mapping for (post, recipient), if any.

    DM reconciliation mirrors the room case but cannot match on room id up
    front (the DM room id is only known after send), so it matches on the
    mapping's target_mxid across all mappings of the post instead.
    """
    for record in await delivery_messages.list_by_discourse_post(
        discourse_post_id=discourse_post_id
    ):
        if record.target_type == "dm" and record.target_mxid == target_mxid:
            return record
    return None


async def deliver_job(
    *,
    job: DeliveryJobRecord,
    discourse_events: DiscourseEventsRepo,
    delivery_messages: Any,
    chat_accounts: ChatAccountsRepo,
    room_links: RoomLinksRepo,
    matrix_client: Any,
    delivery_jobs: DeliveryJobsRepo | None = None,
    audit_logs: AuditLogsRepo | None = None,
    categories: Any | None = None,
    live_e2e_category_id: int | None = None,
) -> WorkerResult:
    event = await discourse_events.get_by_id(job.event_id)
    if event is None:
        await record_audit_entry(
            audit_logs,
            AuditEntry(
                action=ACTION_ROOM_DELIVERY if job.target_type == "room" else ACTION_DM_DELIVERY,
                discourse_username_used=SYSTEM_ACTOR,
                mxid=SYSTEM_ACTOR,
                platform="system",
                matrix_room_id=job.matrix_room_id,
                success=False,
                error_message="missing_discourse_event",
                status=STATUS_FAILED,
            ),
        )
        return WorkerResult(complete=False, error="missing_discourse_event")
    event_category_id = getattr(event, "category_id", None)
    if categories is not None and isinstance(event_category_id, int):
        category = await categories.get_by_discourse_category_id(event_category_id)
        live_exception = (
            live_e2e_category_id is not None and event_category_id == live_e2e_category_id
        )
        if (
            category is None
            or not category.enabled
            or (not category.is_public and not live_exception)
        ):
            logger.warning(
                "Suppressing job %s because category %s is no longer deliverable",
                job.id,
                event_category_id,
            )
            return WorkerResult(complete=True)
    # Reconcile durable evidence of an earlier successful attempt before any
    # device-id check. A rotated device must not strand a job whose mapping is
    # already present.
    if job.target_type == "room" and job.matrix_room_id is not None:
        existing_mapping = await _find_existing_room_mapping(
            delivery_messages=delivery_messages,
            discourse_post_id=event.discourse_post_id,
            matrix_room_id=job.matrix_room_id,
        )
        if existing_mapping is not None:
            return WorkerResult(complete=True)
    if job.target_type == "dm" and job.target_mxid is not None:
        existing_mapping = await _find_existing_dm_mapping(
            delivery_messages=delivery_messages,
            discourse_post_id=event.discourse_post_id,
            target_mxid=job.target_mxid,
        )
        if existing_mapping is not None:
            return WorkerResult(complete=True)
    # Durable idempotency for the post-send/pre-persist window: stamp a stable
    # transaction id BEFORE the Matrix write. If the process dies after the
    # homeserver accepted the event but before create_mapping() persisted, the
    # retry reuses this tx id and the homeserver deduplicates the send instead
    # of posting a duplicate message. Passes through when no jobs repo is given
    # (legacy call sites / tests that only exercise the pre-send lookup).
    matrix_tx_id = (
        await delivery_jobs.ensure_matrix_tx_id(job.id) if delivery_jobs is not None else None
    )
    # Device scoping: Matrix tx ids only deduplicate within the device that
    # issued them. Stamp the client's (stable) device id on the job so a
    # restarted process logging in as the same device keeps the persisted tx
    # id valid; without this a password re-login would mint a new device and
    # the retry could create a second event. ensure_matrix_device_id() is
    # idempotent and returns the FIRST device id ever stamped: if the service
    # was restarted with a DIFFERENT device (changed MATRIX_DEVICE_ID or a
    # replacement access token), the job's durable tx id would no longer
    # deduplicate on the current device — refuse the send (fail closed)
    # instead of silently proceeding.
    if delivery_jobs is not None:
        if not matrix_client.device_id:
            logger.error("Refusing delivery of job %s: Matrix device id is unknown", job.id)
            return WorkerResult(complete=False, error="matrix_device_unknown")
        stored_device_id = await delivery_jobs.ensure_matrix_device_id(
            job.id, device_id=matrix_client.device_id
        )
        if stored_device_id != matrix_client.device_id:
            logger.error(
                "Refusing delivery of job %s: job was stamped for Matrix device "
                "%s but the current client uses device %s; the persisted tx id "
                "would not deduplicate on this device",
                job.id,
                stored_device_id,
                matrix_client.device_id,
            )
            return WorkerResult(complete=False, error="matrix_device_mismatch")
    action = ACTION_ROOM_DELIVERY if job.target_type == "room" else ACTION_DM_DELIVERY

    result_room_id: str | None = None
    target_mxid: str | None = None

    def build_entry(
        matrix_event_id: str | None, error: str | None, *, status: str = STATUS_SUCCESS
    ) -> AuditEntry:
        # Pending attempt rows carry success=None: the outcome is unknown until
        # update_audit_outcome runs, and a crash before that must not leave a
        # durable row that legacy `success = TRUE` queries count as delivered.
        success: bool | None
        if status == STATUS_PENDING and error is None:
            success = None
        else:
            success = error is None
        return AuditEntry(
            action=action,
            discourse_username_used=SYSTEM_ACTOR,
            mxid=target_mxid,
            platform="system",
            topic_id=event.discourse_topic_id,
            post_id=event.discourse_post_id,
            matrix_room_id=result_room_id,
            matrix_event_id=matrix_event_id,
            success=success,
            error_message=error,
            status=status,
        )

    async def audit_attempt() -> int | None:
        # Live write path: audit must be wired, or the send is refused before
        # any unrecorded external write can happen. Called only AFTER all
        # local preparation has succeeded, immediately before the external
        # write: a pre-send failure must never leave a pending success=NULL
        # row, which is indistinguishable from a crash during the write.
        return await record_audit_attempt(
            audit_logs, build_entry(None, None, status=STATUS_PENDING), require_logger=True
        )

    if job.target_type == "room" and job.matrix_room_id is not None:
        result_room_id = job.matrix_room_id
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
        audit_log_id = await audit_attempt()
        send_kwargs: dict[str, Any] = {"formatted": formatted}
        if matrix_tx_id is not None:
            send_kwargs["tx_id"] = matrix_tx_id
        try:
            if parent_mapping is not None:
                result = await matrix_client.send_reply(
                    job.matrix_room_id,
                    rendered_body,
                    parent_mapping.matrix_event_id,
                    **send_kwargs,
                )
            else:
                result = await matrix_client.send_text(
                    job.matrix_room_id,
                    rendered_body,
                    **send_kwargs,
                )
        except Exception as exc:
            await update_audit_outcome(
                audit_logs, audit_log_id, success=False, error_message=failure_reason(exc)
            )
            raise
        await update_audit_outcome(
            audit_logs,
            audit_log_id,
            success=True,
            error_message=None,
            post_id=event.discourse_post_id,
            matrix_event_id=result.event_id,
        )
        try:
            await delivery_messages.create_mapping(
                discourse_topic_id=event.discourse_topic_id,
                discourse_post_id=event.discourse_post_id,
                matrix_room_id=job.matrix_room_id,
                matrix_event_id=result.event_id,
                target_type="room",
                target_mxid=None,
                parent_delivery_message_id=parent_mapping.id
                if parent_mapping is not None
                else None,
            )
        except Exception:
            # The Matrix send already went out; do NOT re-raise (which would only
            # schedule a retry that the pre-send lookup cannot protect). The
            # retryable failure keeps the job alive and the durable tx id stamped
            # on the job makes the eventual re-send deduplicate server-side.
            logger.warning(
                "Failed to persist delivery mapping for job %s after Matrix send; "
                "retry will reuse the job's durable tx id",
                job.id,
            )
            return WorkerResult(complete=False, error="mapping_persistence_failed")
        return WorkerResult(complete=True)
    if job.target_type == "dm" and job.target_mxid is not None:
        account = await chat_accounts.get_by_mxid(job.target_mxid)
        locale = account.response_locale if account is not None else "en"
        body, formatted = _render_delivery_content(event.raw_payload_json, full_content=True)
        # Endpoint scoping: tx ids deduplicate per /rooms/{roomId}/send endpoint.
        # Re-resolving the DM room on a retry could select or create a DIFFERENT
        # room, sending the same tx id to a different endpoint where it cannot
        # deduplicate. Resolve the room FIRST, persist the pin on the job BEFORE
        # any Matrix write, then send to the pinned room. Pinning before the
        # send closes the crash window: if the process dies after the homeserver
        # accepted the event but before any post-send DB write, the retry still
        # sends to the SAME room and the durable tx id deduplicates server-side.
        if delivery_jobs is not None:
            pinned_room_id = await delivery_jobs.get_matrix_dm_room_id(job.id)
            if pinned_room_id is None:
                resolved_room_id = await matrix_client.resolve_dm_room(job.target_mxid)
                # A concurrent worker may have won the pin with a different
                # room. Always send to the repository's durable winner.
                pinned_room_id = await delivery_jobs.pin_matrix_dm_room(
                    job.id, room_id=resolved_room_id
                )
        else:
            # Legacy call sites without a jobs repo: resolve without a pin.
            pinned_room_id = await matrix_client.resolve_dm_room(job.target_mxid)
        target_mxid = job.target_mxid
        result_room_id = pinned_room_id
        audit_log_id = await audit_attempt()
        send_kwargs: dict[str, Any] = {"formatted": formatted}
        if matrix_tx_id is not None:
            send_kwargs["tx_id"] = matrix_tx_id
        try:
            result = await matrix_client.send_dm(
                pinned_room_id,
                body or translate("pairing.unpaired", locale),
                **send_kwargs,
            )
        except Exception as exc:
            await update_audit_outcome(
                audit_logs, audit_log_id, success=False, error_message=failure_reason(exc)
            )
            raise
        room_id = result.room_id or pinned_room_id
        if room_id is None:
            await update_audit_outcome(
                audit_logs,
                audit_log_id,
                success=False,
                error_message="missing_dm_room_id",
            )
            return WorkerResult(complete=False, error="missing_dm_room_id")
        await update_audit_outcome(
            audit_logs,
            audit_log_id,
            success=True,
            error_message=None,
            post_id=event.discourse_post_id,
            matrix_event_id=result.event_id,
            matrix_room_id=room_id,
        )
        try:
            await delivery_messages.create_mapping(
                discourse_topic_id=event.discourse_topic_id,
                discourse_post_id=event.discourse_post_id,
                matrix_room_id=room_id,
                matrix_event_id=result.event_id,
                target_type="dm",
                target_mxid=job.target_mxid,
                parent_delivery_message_id=None,
            )
        except Exception:
            # Same post-send/pre-persist caveat as the room branch: the DM was
            # already sent; the durable tx id makes the retry deduplicate.
            logger.warning(
                "Failed to persist DM mapping for job %s after Matrix send; "
                "retry will reuse the job's durable tx id",
                job.id,
            )
            return WorkerResult(complete=False, error="mapping_persistence_failed")
        return WorkerResult(complete=True)
    await record_audit_entry(
        audit_logs,
        AuditEntry(
            action=action,
            discourse_username_used=SYSTEM_ACTOR,
            mxid=target_mxid,
            platform="system",
            topic_id=event.discourse_topic_id,
            post_id=event.discourse_post_id,
            success=False,
            error_message="unsupported_delivery_target",
            status=STATUS_FAILED,
        ),
    )
    return WorkerResult(complete=False, error="unsupported_delivery_target")
