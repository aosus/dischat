from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from dischat.bridge import handle_matrix_reply
from dischat.commands.parser import parse_command
from dischat.matrix.client import MatrixMessage, MatrixSendResult
from dischat.security.audit import (
    ACTION_PAIRING_PM,
    ACTION_SEND_MATRIX_NOTICE,
    STATUS_PENDING,
    AuditEntry,
    AuditLogsRepo,
    failure_reason,
    record_audit_attempt,
    update_audit_outcome,
)
from dischat.security.permissions import detect_platform
from dischat.storage.repositories import new_lease_owner


async def _deliver_fenced_command(
    *,
    matrix_client: Any,
    discourse_client,
    event_state,
    audit_logs: AuditLogsRepo | None,
    sender_mxid: str,
    sender_platform: str,
    room_id: str,
    event_id: str,
    run_command: Callable[[], Awaitable[CommandResponseLike | None]],
) -> None:
    """Run a command behind the same durable fence the reply path uses.

    Fence protocol (only when ``event_state`` is configured):
      - seed the marker with an exclusive lease (or take over a stale
        orphaned lease) BEFORE running the command, so a replay never
        re-executes the command's side effects;
      - run the command; for pairing commands this creates the Discourse PM
        (the external write);
      - after the command's side effects are done — for every command, not
        only PM deliveries — record the ``written`` outcome together with
        the notice text still owed to the room, BEFORE sending the notice;
      - send the notice, then mark the event ``processed`` under the lease.

    Reconciliation on replay:
      - stale ``claimed`` marker (predecessor crashed before the command's
        side effects, lease lapsed) → atomic lease takeover, then run the
        command fresh, delivering a valid code;
      - ``owned`` marker → a live attempt holds the fence; do nothing;
      - ``written`` marker → the command already ran and its side effects
        (e.g. the pairing PM) exist; finish only the pending room notice
        stored on the marker, never re-running the command;
      - ``processed`` → nothing left to do.
    """
    if event_state is None:
        command_response = await run_command()
        if command_response is None:
            return
        await _deliver_command_response(
            discourse_client=discourse_client,
            matrix_client=matrix_client,
            audit_logs=audit_logs,
            sender_mxid=sender_mxid,
            sender_platform=sender_platform,
            command_response_body=command_response.body,
            pairing_code_to_deliver=command_response.pairing_code_to_deliver,
            pairing_target_username=command_response.pairing_target_username,
            room_id=room_id,
        )
        return

    lease_owner = new_lease_owner()
    claimed = await event_state.claim_event(
        matrix_room_id=room_id,
        matrix_event_id=event_id,
        lease_owner=lease_owner,
    )
    if claimed is None:
        marker = await event_state.get_event(matrix_room_id=room_id, matrix_event_id=event_id)
        if marker is not None and marker.status in ("claimed", "owned"):
            # Exclusive takeover of a demonstrably stale fence: the row moves
            # to 'owned' under this attempt's token, so exactly one racing
            # replay can win. The repository only releases a fence whose
            # lease has lapsed (a crashed predecessor), never one a live
            # worker still holds.
            adopted = await event_state.adopt_event(
                matrix_room_id=room_id,
                matrix_event_id=event_id,
                lease_owner=lease_owner,
            )
            if adopted is None:
                # A live worker or a concurrent replay owns the command's
                # side effects now.
                return
            # Fence taken over: fall through and run the command.
        elif marker is not None and marker.status in ("written", "processed"):
            if marker.status == "processed":
                return
            # 'written': the predecessor already ran the command and its side
            # effects exist. Finish only the pending notice; never re-run the
            # command. The recorded outcome cleared the lease columns, so the
            # confirm must not carry this attempt's token (it would never
            # match and the marker would stay 'written' forever).
            if marker.response_notice is not None:
                await _deliver_matrix_notice(
                    matrix_client=matrix_client,
                    audit_logs=audit_logs,
                    sender_mxid=sender_mxid,
                    sender_platform=sender_platform,
                    body=marker.response_notice,
                    room_id=room_id,
                    pairing_target_username=None,
                )
            await event_state.mark_event_processed(
                matrix_room_id=room_id, matrix_event_id=event_id, lease_owner=None
            )
            return
        else:
            # No marker (released between the claim and this read, or removed
            # manually); retry the claim and process as a fresh attempt.
            if (
                await event_state.claim_event(
                    matrix_room_id=room_id,
                    matrix_event_id=event_id,
                    lease_owner=lease_owner,
                )
                is None
            ):
                return

    try:
        command_response = await run_command()
        if command_response is None:
            await event_state.mark_event_processed(
                matrix_room_id=room_id, matrix_event_id=event_id, lease_owner=lease_owner
            )
            return
        # Pairing-specific delivery first: the code goes to the user's
        # Discourse DM, not the room notice. The PM is the command's external
        # write and completes before the outcome is recorded, so a 'written'
        # marker never promises a PM that was not sent; a crash after the PM
        # leaves a marker whose replay delivers the stored notice without
        # re-running the command (no second PM).
        await _deliver_pairing_pm(
            discourse_client=discourse_client,
            audit_logs=audit_logs,
            sender_mxid=sender_mxid,
            sender_platform=sender_platform,
            pairing_code=command_response.pairing_code_to_deliver,
            pairing_target_username=command_response.pairing_target_username,
        )
        # Record the outcome for EVERY command (not just PM deliveries)
        # BEFORE the room notice: the fenced guarantee is that a replay
        # never re-executes the command itself, and commands like /unpair,
        # /watch, or /unwatch mutate durable pairing/watch state. Recording
        # here means a notice-send failure releases nothing — the fence is
        # 'written' and release_event cannot delete it — so a replay
        # reconciles the pending notice instead of re-running the command.
        outcome = await event_state.mark_event_written(
            matrix_room_id=room_id,
            matrix_event_id=event_id,
            response_notice=command_response.body,
            lease_owner=lease_owner,
        )
        if not outcome.recorded:
            # This attempt lost the fence while its command was in flight;
            # the winner owns the command's side effects and its pending
            # notice. Never treat our response as the source of truth.
            return
        await _deliver_matrix_notice(
            matrix_client=matrix_client,
            audit_logs=audit_logs,
            sender_mxid=sender_mxid,
            sender_platform=sender_platform,
            body=command_response.body,
            room_id=room_id,
            pairing_target_username=command_response.pairing_target_username,
        )
    except Exception:
        # Failed before run_command() returned (no external write happened
        # in this attempt) or while sending the notice with the outcome
        # already recorded — in the notice case the fence is 'written' and
        # release_event cannot delete it, and in the command case the release
        # only lands while this attempt still holds the lease, so either way
        # a replay can never re-run the command's side effects.
        await event_state.release_event(
            matrix_room_id=room_id,
            matrix_event_id=event_id,
            lease_owner=lease_owner,
        )
        raise
    # The outcome was recorded (and the lease columns cleared) before the
    # notice, so this confirm is a guarded tokenless transition from
    # 'written' to 'processed'.
    await event_state.mark_event_processed(
        matrix_room_id=room_id, matrix_event_id=event_id, lease_owner=None
    )


class PairingPmWriter(Protocol):
    async def create_private_message(
        self,
        *,
        target_username: str,
        title: str,
        raw: str,
    ) -> object: ...


class MatrixSyncClient(Protocol):
    user_id: str

    def extract_messages(self, sync_response: object) -> list[MatrixMessage]: ...

    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult: ...


class CommandService(Protocol):
    async def handle_message(
        self,
        *,
        mxid: str,
        platform: str,
        body: str,
        locale: str,
        live_e2e_category_id: int | None = None,
    ) -> CommandResponseLike | None: ...


class CommandResponseLike(Protocol):
    body: str
    pairing_code_to_deliver: str | None
    pairing_target_username: str | None


async def process_sync_messages(
    *,
    matrix_client: Any,
    service: CommandService,
    discourse_client,
    chat_accounts,
    room_links,
    delivery_messages,
    audit_logs: AuditLogsRepo | None = None,
    event_state=None,
    relay_matrix_username: str,
    relay_telegram_username: str,
    relay_discord_username: str,
    live_e2e_category_id: int | None,
    sync_response: object,
) -> None:
    for message in matrix_client.extract_messages(sync_response):
        if message.sender == matrix_client.user_id:
            continue
        if parse_command(message.body) is not None:
            # Side-effecting commands get the same durable fence as replies:
            # without it, a crash after the pairing PM but before the sync
            # token advanced would replay the command and send a second PM.
            await _deliver_fenced_command(
                matrix_client=matrix_client,
                discourse_client=discourse_client,
                event_state=event_state,
                audit_logs=audit_logs,
                sender_mxid=message.sender,
                sender_platform=detect_platform(message.sender),
                room_id=message.room_id,
                event_id=message.event_id,
                run_command=lambda message=message: service.handle_message(
                    mxid=message.sender,
                    platform=detect_platform(message.sender),
                    body=message.body,
                    locale="ar",
                    live_e2e_category_id=live_e2e_category_id,
                ),
            )
            continue
        command_response = await service.handle_message(
            mxid=message.sender,
            platform=detect_platform(message.sender),
            body=message.body,
            locale="ar",
            live_e2e_category_id=live_e2e_category_id,
        )
        if command_response is not None:
            await _deliver_command_response(
                discourse_client=discourse_client,
                matrix_client=matrix_client,
                audit_logs=audit_logs,
                sender_mxid=message.sender,
                sender_platform=detect_platform(message.sender),
                command_response_body=command_response.body,
                pairing_code_to_deliver=command_response.pairing_code_to_deliver,
                pairing_target_username=command_response.pairing_target_username,
                room_id=message.room_id,
            )
            continue
        if message.parent_event_id is None:
            continue
        await handle_matrix_reply(
            message=message,
            discourse_client=discourse_client,
            matrix_client=matrix_client,
            chat_accounts=chat_accounts,
            room_links=room_links,
            delivery_messages=delivery_messages,
            audit_logs=audit_logs,
            event_state=event_state,
            relay_matrix_username=relay_matrix_username,
            relay_telegram_username=relay_telegram_username,
            relay_discord_username=relay_discord_username,
        )


async def _deliver_pairing_pm(
    *,
    discourse_client,
    audit_logs: AuditLogsRepo | None,
    sender_mxid: str,
    sender_platform: str,
    pairing_code: str | None,
    pairing_target_username: str | None,
) -> None:
    if not pairing_code or not pairing_target_username:
        return
    attempt_id = await record_audit_attempt(
        audit_logs,
        AuditEntry(
            action=ACTION_PAIRING_PM,
            discourse_username_used=pairing_target_username,
            mxid=sender_mxid,
            platform=sender_platform,
            success=None,
            status=STATUS_PENDING,
        ),
        require_logger=True,
    )
    try:
        await discourse_client.create_private_message(
            target_username=pairing_target_username,
            title="Dischat pairing code",
            raw=pairing_code,
        )
    except Exception as exc:
        await update_audit_outcome(
            audit_logs, attempt_id, success=False, error_message=failure_reason(exc)
        )
        raise
    await update_audit_outcome(audit_logs, attempt_id, success=True, error_message=None)


async def _deliver_matrix_notice(
    *,
    matrix_client: Any,
    audit_logs: AuditLogsRepo | None,
    sender_mxid: str,
    sender_platform: str,
    body: str,
    pairing_target_username: str | None,
    room_id: str,
) -> None:
    attempt_id = await record_audit_attempt(
        audit_logs,
        AuditEntry(
            action=ACTION_SEND_MATRIX_NOTICE,
            discourse_username_used=pairing_target_username or "",
            mxid=sender_mxid,
            platform=sender_platform,
            matrix_room_id=room_id,
            success=None,
            status=STATUS_PENDING,
        ),
        require_logger=True,
    )
    try:
        notice_result = await matrix_client.send_notice(room_id, body)
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
        matrix_event_id=notice_result.event_id,
    )


async def _deliver_command_response(
    *,
    discourse_client,
    matrix_client: Any,
    audit_logs: AuditLogsRepo | None,
    sender_mxid: str,
    sender_platform: str,
    command_response_body: str,
    pairing_code_to_deliver: str | None,
    pairing_target_username: str | None,
    room_id: str,
) -> None:
    await _deliver_pairing_pm(
        discourse_client=discourse_client,
        audit_logs=audit_logs,
        sender_mxid=sender_mxid,
        sender_platform=sender_platform,
        pairing_code=pairing_code_to_deliver,
        pairing_target_username=pairing_target_username,
    )
    await _deliver_matrix_notice(
        matrix_client=matrix_client,
        audit_logs=audit_logs,
        sender_mxid=sender_mxid,
        sender_platform=sender_platform,
        body=command_response_body,
        pairing_target_username=pairing_target_username,
        room_id=room_id,
    )
