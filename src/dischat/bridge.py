from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from dischat.i18n import translate
from dischat.matrix.client import MatrixMessage, MatrixSendResult, event_notice_tx_id
from dischat.security.audit import (
    ACTION_DISCOURSE_REPLY,
    ACTION_SEND_MATRIX_NOTICE,
    STATUS_PENDING,
    AuditEntry,
    failure_reason,
    record_audit_attempt,
    update_audit_outcome,
)
from dischat.security.permissions import can_post_from_chat, detect_platform
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryMessageRecord,
    MatrixEventStatus,
    RoomLinkRecord,
    TargetType,
    new_lease_owner,
)


@dataclass(slots=True, frozen=True)
class BridgeResult:
    posted: bool
    discourse_username: str | None = None
    discourse_post_id: int | None = None
    error_message: str | None = None
    matrix_response: MatrixSendResult | None = None


class DiscourseReplyResult(Protocol):
    topic_id: int
    post_id: int


class DiscourseReplyWriter(Protocol):
    async def get_post(self, post_id: int) -> dict[str, object]: ...

    async def create_reply(
        self,
        *,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        api_username: str | None = None,
    ) -> DiscourseReplyResult: ...


class DiscourseTopicReader(Protocol):
    async def get_topic(self, topic_id: int) -> dict[str, object]: ...


class ChatAccountsRepo(Protocol):
    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount: ...


class RoomLinksRepo(Protocol):
    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None: ...


class DeliveryMessagesRepo(Protocol):
    async def get_by_matrix_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
    ) -> DeliveryMessageRecord | None: ...

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


class MatrixEventMarker(Protocol):
    """Durable state of a Matrix event's fence marker."""

    status: MatrixEventStatus
    discourse_topic_id: int | None
    discourse_post_id: int | None
    response_notice: str | None


class EventOutcomeResult(Protocol):
    """Result of recording an external write in the durable ledger."""

    recorded: bool
    conflicting_post_id: int | None


class MatrixEventStateRepo(Protocol):
    async def claim_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str | None = None,
        lease_seconds: int = ...,
    ) -> MatrixEventMarker | None: ...

    async def adopt_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str,
        lease_seconds: int = ...,
    ) -> MatrixEventMarker | None: ...

    async def begin_event_write(
        self, *, matrix_room_id: str, matrix_event_id: str, lease_owner: str
    ) -> bool: ...

    async def mark_event_written(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        discourse_topic_id: int | None = None,
        discourse_post_id: int | None = None,
        response_notice: str | None = None,
        lease_owner: str | None = None,
    ) -> EventOutcomeResult: ...

    async def mark_event_processed(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str | None = None,
    ) -> None: ...

    async def get_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> MatrixEventMarker | None: ...

    async def release_event(
        self, *, matrix_room_id: str, matrix_event_id: str, lease_owner: str | None = None
    ) -> None: ...

    async def reconcile_event_from_mapping(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> None: ...


class MatrixNoticeClient(Protocol):
    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult: ...


def _bridge_result_for_existing_reply(mapping: DeliveryMessageRecord | None) -> BridgeResult:
    """Result for an event whose durable marker already existed.

    No Discourse write is ever attempted in this state. When the predecessor
    attempt managed to commit its delivery mapping we surface its post id;
    otherwise the fence is still held by another (possibly crashed) attempt
    and we report that the event is already being handled.
    """
    if mapping is not None:
        return BridgeResult(posted=False, discourse_post_id=mapping.discourse_post_id)
    return BridgeResult(posted=False, error_message="event_already_claimed")


async def _release_event_fence(
    event_state: MatrixEventStateRepo | None,
    *,
    room_id: str,
    event_id: str,
    lease_owner: str | None = None,
) -> None:
    if event_state is None:
        return
    await event_state.release_event(
        matrix_room_id=room_id, matrix_event_id=event_id, lease_owner=lease_owner
    )


async def _reconcile_written_event(
    event_state: MatrixEventStateRepo,
    delivery_messages: DeliveryMessagesRepo,
    *,
    room_id: str,
    event_id: str,
) -> BridgeResult:
    """Reconcile a replay of an event whose external write already happened.

    A ``written``/``processed`` marker is proof the Discourse reply was
    created, even if the process died before the delivery mapping committed.
    Re-create the mapping from the outcome columns so the reply stays
    addressable, then return its post id. Never write to Discourse again.
    """
    mapping = await delivery_messages.get_by_matrix_event(
        matrix_room_id=room_id,
        matrix_event_id=event_id,
    )
    if mapping is not None:
        await event_state.mark_event_processed(matrix_room_id=room_id, matrix_event_id=event_id)
        return BridgeResult(posted=False, discourse_post_id=mapping.discourse_post_id)

    event = await event_state.get_event(matrix_room_id=room_id, matrix_event_id=event_id)
    topic_id = event.discourse_topic_id if event is not None else None
    post_id = event.discourse_post_id if event is not None else None
    if not isinstance(topic_id, int) or not isinstance(post_id, int):
        # No usable outcome recorded (crash before the write returned, or a
        # legacy marker from before migration 0004): treat like any other
        # fenced event rather than guessing a post id.
        return BridgeResult(posted=False, error_message="event_already_claimed")

    # The parent link is metadata only and may be unrecoverable after a crash;
    # what matters is that the reply itself becomes addressable again without
    # a second Discourse write.
    created = await delivery_messages.create_mapping(
        discourse_topic_id=topic_id,
        discourse_post_id=post_id,
        matrix_room_id=room_id,
        matrix_event_id=event_id,
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )
    await event_state.mark_event_processed(matrix_room_id=room_id, matrix_event_id=event_id)
    return BridgeResult(posted=False, discourse_post_id=created.discourse_post_id)


def relay_username_for_platform(*, platform: str, matrix: str, telegram: str, discord: str) -> str:
    if platform == "telegram":
        return telegram
    if platform == "discord":
        return discord
    return matrix


async def _send_notice_with_audit(
    *,
    matrix_client: MatrixNoticeClient,
    audit_logs: AuditLogsRepo,
    room_id: str,
    body: str,
    mxid: str,
    platform: str,
    tx_id: str | None = None,
) -> MatrixSendResult:
    """Send a Matrix notice as an audited live write (attempt-first)."""
    attempt_id = await record_audit_attempt(
        audit_logs,
        AuditEntry(
            action=ACTION_SEND_MATRIX_NOTICE,
            discourse_username_used="",
            mxid=mxid,
            platform=platform,
            matrix_room_id=room_id,
            success=None,
            status=STATUS_PENDING,
        ),
        require_logger=True,
    )
    try:
        result = await matrix_client.send_notice(room_id, body, tx_id=tx_id)
    except Exception as exc:
        await update_audit_outcome(
            audit_logs, attempt_id, success=False, error_message=failure_reason(exc)
        )
        raise
    await update_audit_outcome(
        audit_logs,
        attempt_id,
        success=True,
        error_message=None,
        matrix_event_id=result.event_id,
    )
    return result


async def handle_matrix_reply(
    *,
    message: MatrixMessage,
    discourse_client: DiscourseReplyWriter,
    matrix_client: MatrixNoticeClient,
    chat_accounts: ChatAccountsRepo,
    room_links: RoomLinksRepo,
    delivery_messages: DeliveryMessagesRepo,
    audit_logs: Any,
    event_state: MatrixEventStateRepo | None = None,
    relay_matrix_username: str,
    relay_telegram_username: str,
    relay_discord_username: str,
) -> BridgeResult:
    if message.parent_event_id is None:
        return BridgeResult(posted=False)

    # A delivery mapping is stronger evidence than the replay ledger: it
    # proves this exact Matrix event already produced a Discourse post. Check
    # it before inserting a fresh fence so retention or an operator cleanup
    # can never reopen a duplicate-write path.
    existing_reply_mapping = await delivery_messages.get_by_matrix_event(
        matrix_room_id=message.room_id,
        matrix_event_id=message.event_id,
    )
    if existing_reply_mapping is not None:
        if event_state is not None:
            reconcile = getattr(event_state, "reconcile_event_from_mapping", None)
            if reconcile is not None:
                await reconcile(
                    matrix_room_id=message.room_id,
                    matrix_event_id=message.event_id,
                )
        return _bridge_result_for_existing_reply(existing_reply_mapping)

    # Durable idempotency fence: seed the marker and take an exclusive
    # processing lease BEFORE any Discourse write. The lease owner token is
    # this attempt's proof of ownership: only the lease holder may perform
    # the external write path, and every downstream ledger call is guarded by
    # the token so a superseded attempt can neither stamp its own outcome nor
    # tear down the winner's fence.
    if event_state is None:
        lease_owner = None
    else:
        lease_owner = new_lease_owner()
        claimed = await event_state.claim_event(
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
            lease_owner=lease_owner,
        )
        if claimed is None:
            # The marker already exists. Decide from its durable state:
            #   - delivery mapping committed → surface the existing post;
            #   - a stale 'claimed' marker with no recorded outcome means a
            #     predecessor crashed before any external write → take the
            #     fence over atomically and deliver the event now
            #     (crash-point (a) recovery);
            #   - a 'written'/'processed' marker means the external write
            #     already happened → reconcile the mapping from the recorded
            #     outcome; never write twice;
            #   - no marker at all (deleted between the failed claim and this
            #     read) → re-claim and deliver as a fresh attempt.
            event = await event_state.get_event(
                matrix_room_id=message.room_id, matrix_event_id=message.event_id
            )
            if event is not None and event.status == "claimed":
                # Exclusive takeover: the stale claim receives this attempt's
                # fresh lease token, so exactly one racing replay can win. It
                # stays claimed until begin_event_write enters the terminal
                # ambiguous-write state; owned/written/processed markers are
                # never adopted.
                adopted = await event_state.adopt_event(
                    matrix_room_id=message.room_id,
                    matrix_event_id=message.event_id,
                    lease_owner=lease_owner,
                )
                if adopted is None:
                    # The fence belongs to another attempt: a live worker
                    # still holds a fresh lease, or a concurrent replay won
                    # the takeover. It owns the event's side effects now.
                    return BridgeResult(posted=False, error_message="event_already_claimed")
                # Fence taken over: fall through and deliver the event.
            elif event is not None and event.status in ("written", "processed"):
                # 'written'/'processed': the external write already happened
                # and its outcome is recorded. Reconcile the mapping from the
                # recorded outcome; never write to Discourse again.
                return await _reconcile_written_event(
                    event_state,
                    delivery_messages,
                    room_id=message.room_id,
                    event_id=message.event_id,
                )
            elif event is not None and event.status == "owned":
                return BridgeResult(posted=False, error_message="event_write_ambiguous")
            else:
                # No marker (e.g. deleted between the failed claim and this
                # read). Re-seed the fence: reconcile would wrongly confirm
                # an event this attempt never wrote.
                if (
                    await event_state.claim_event(
                        matrix_room_id=message.room_id,
                        matrix_event_id=message.event_id,
                        lease_owner=lease_owner,
                    )
                    is None
                ):
                    # A racing attempt re-claimed it first; it owns the event
                    # now, exactly as if the original claim had returned None.
                    return BridgeResult(posted=False, error_message="event_already_claimed")
                # Fence (re-)claimed: fall through and deliver the event.

    parent = await delivery_messages.get_by_matrix_event(
        matrix_room_id=message.room_id,
        matrix_event_id=message.parent_event_id,
    )
    if parent is None:
        await _release_event_fence(
            event_state, room_id=message.room_id, event_id=message.event_id, lease_owner=lease_owner
        )
        return BridgeResult(posted=False, error_message="parent_mapping_pending")

    account = await chat_accounts.ensure_account(
        mxid=message.sender,
        platform=detect_platform(message.sender),
        response_locale="ar",
    )
    room_link = await room_links.get_by_room_id(message.room_id)
    permission = can_post_from_chat(
        is_dm=room_link is None,
        has_parent_bridge_message=True,
        is_paired=account.discourse_username is not None and account.revoked_at is None,
        room_allows_relay=room_link.allow_relay if room_link is not None else False,
    )

    if permission.decision == "reject":
        response = await _send_notice_with_audit(
            matrix_client=matrix_client,
            audit_logs=audit_logs,
            room_id=message.room_id,
            body=translate("posting.requires_pairing", account.response_locale),
            mxid=message.sender,
            platform=account.platform,
            tx_id=event_notice_tx_id(message.room_id, message.event_id),
        )
        await _release_event_fence(
            event_state, room_id=message.room_id, event_id=message.event_id, lease_owner=lease_owner
        )
        return BridgeResult(posted=False, error_message=permission.reason, matrix_response=response)
    if permission.decision == "ignore":
        await _release_event_fence(
            event_state, room_id=message.room_id, event_id=message.event_id, lease_owner=lease_owner
        )
        return BridgeResult(posted=False)

    discourse_username = account.discourse_username
    if permission.decision == "relay":
        discourse_username = relay_username_for_platform(
            platform=account.platform,
            matrix=relay_matrix_username,
            telegram=relay_telegram_username,
            discord=relay_discord_username,
        )
    assert discourse_username is not None
    try:
        parent_post = await discourse_client.get_post(parent.discourse_post_id)
    except httpx.HTTPError:
        parent_post = {}
    reply_to_post_number = parent_post.get("post_number")
    if not isinstance(reply_to_post_number, int):
        if hasattr(discourse_client, "get_topic"):
            topic_payload = await cast("DiscourseTopicReader", discourse_client).get_topic(
                parent.discourse_topic_id
            )
            post_stream = topic_payload.get("post_stream")
            topic_posts: list[dict[str, Any]] = []
            if isinstance(post_stream, dict):
                post_stream_dict = cast("dict[str, Any]", post_stream)
                posts = post_stream_dict.get("posts")
                if isinstance(posts, list):
                    topic_posts = [post for post in posts if isinstance(post, dict)]
            for topic_post in topic_posts:
                if topic_post.get("id") == parent.discourse_post_id:
                    reply_to_post_number = topic_post.get("post_number")
                    break
    if not isinstance(reply_to_post_number, int):
        response = await _send_notice_with_audit(
            matrix_client=matrix_client,
            audit_logs=audit_logs,
            room_id=message.room_id,
            body=translate("posting.requires_pairing", account.response_locale),
            mxid=message.sender,
            platform=account.platform,
            tx_id=event_notice_tx_id(message.room_id, message.event_id),
        )
        await _release_event_fence(
            event_state, room_id=message.room_id, event_id=message.event_id, lease_owner=lease_owner
        )
        return BridgeResult(
            posted=False, error_message="missing_parent_post_number", matrix_response=response
        )

    # Claim/adopt (lease) → write → record outcome → map → confirm.
    #
    # The marker row is the fence, and the write outcome (Discourse topic/post
    # ids) is recorded in it immediately after the external write returns,
    # BEFORE the delivery mapping is attempted. This resolves the two-sided
    # ambiguity of a bare claim:
    #   - crash after claim / before the write → marker is still 'claimed'
    #     with no outcome; once the lease lapses a replay takes it over and
    #     delivers the event;
    #   - crash after the write / before the mapping → marker is 'written'
    #     with the outcome; a replay rebuilds the mapping instead of writing
    #     a duplicate Discourse post.
    #
    # external_write_done flips only after create_reply() returns. The marker
    # enters owned immediately before that call; any transport failure is
    # ambiguous and remains fenced for operator reconciliation. Failures after
    # the call returns also preserve the written/owned fence.
    external_write_done = False
    reply_audit_id = await record_audit_attempt(
        audit_logs,
        AuditEntry(
            action=ACTION_DISCOURSE_REPLY,
            mxid=message.sender,
            platform=account.platform,
            discourse_username_used=discourse_username or "",
            topic_id=parent.discourse_topic_id,
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
            success=None,
            status=STATUS_PENDING,
        ),
        require_logger=True,
    )
    if event_state is not None and lease_owner is not None:
        begin_write = getattr(event_state, "begin_event_write", None)
        if begin_write is None or not await begin_write(
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
            lease_owner=lease_owner,
        ):
            await update_audit_outcome(
                audit_logs,
                reply_audit_id,
                success=False,
                error_message="event_fence_lost_before_write",
            )
            return BridgeResult(posted=False, error_message="event_already_claimed")
    try:
        write_result = await discourse_client.create_reply(
            topic_id=parent.discourse_topic_id,
            raw=message.body,
            reply_to_post_number=reply_to_post_number,
            api_username=discourse_username,
        )
        external_write_done = True
        if event_state is not None:
            outcome = await event_state.mark_event_written(
                matrix_room_id=message.room_id,
                matrix_event_id=message.event_id,
                discourse_topic_id=write_result.topic_id,
                discourse_post_id=write_result.post_id,
                lease_owner=lease_owner,
            )
            if not outcome.recorded:
                # Another attempt owns the fence and already recorded its own
                # write for this event (this attempt was superseded by a lease
                # takeover while its HTTP call was in flight). Surface the
                # winner's post and never map or confirm ours, so the loser
                # cannot shadow the winner.
                return BridgeResult(
                    posted=False,
                    error_message="event_already_claimed",
                    discourse_post_id=outcome.conflicting_post_id,
                )
        # Resolve the audit attempt after the durable event outcome. If this
        # local update fails, the event fence still prevents a duplicate
        # Discourse write and the pending audit row truthfully signals an
        # outcome that needs reconciliation.
        await update_audit_outcome(
            audit_logs,
            reply_audit_id,
            success=True,
            error_message=None,
            post_id=write_result.post_id,
        )
        await delivery_messages.create_mapping(
            discourse_topic_id=write_result.topic_id,
            discourse_post_id=write_result.post_id,
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
            target_type="room",
            target_mxid=None,
            parent_delivery_message_id=parent.id,
        )
    except Exception as exc:
        if event_state is None and not external_write_done:
            # Compatibility for unfenced test/adapter paths: without a
            # durable marker there is no reconciliation state to preserve.
            await update_audit_outcome(
                audit_logs, reply_audit_id, success=False, error_message=failure_reason(exc)
            )
        # Transport failures after entering ``owned`` are ambiguous: the
        # server may have committed the reply before the connection failed.
        # Leave both the fence and audit attempt pending for reconciliation.
        raise

    if event_state is not None:
        # The outcome was recorded above (outcome.recorded is required True to
        # reach this point), and recording it cleared the lease columns: a
        # 'written' marker is terminal for adoption and needs no lease. The
        # confirm therefore carries no token — a guarded tokenless confirm.
        await event_state.mark_event_processed(
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
        )
    return BridgeResult(
        posted=True,
        discourse_username=discourse_username,
        discourse_post_id=write_result.post_id,
    )
