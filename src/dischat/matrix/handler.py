from __future__ import annotations

from dischat.bridge import handle_matrix_reply
from dischat.matrix.client import NioMatrixClient
from dischat.security.permissions import detect_platform
from dischat.service import DischatService


async def process_sync_messages(
    *,
    matrix_client: NioMatrixClient,
    service: DischatService,
    discourse_client,
    chat_accounts,
    room_links,
    delivery_messages,
    audit_logs,
    relay_matrix_username: str,
    relay_telegram_username: str,
    relay_discord_username: str,
    live_e2e_category_id: int | None,
    sync_response,
) -> None:
    for message in matrix_client.extract_messages(sync_response):
        if message.sender == matrix_client.user_id:
            continue
        command_response = await service.handle_message(
            mxid=message.sender,
            platform=detect_platform(message.sender),
            body=message.body,
            locale='ar',
            live_e2e_category_id=live_e2e_category_id,
        )
        if command_response is not None:
            if command_response.pairing_code_to_deliver and command_response.pairing_target_username:
                await discourse_client.create_private_message(
                    target_username=command_response.pairing_target_username,
                    title='Dischat pairing code',
                    raw=command_response.pairing_code_to_deliver,
                )
            await matrix_client.send_notice(message.room_id, command_response.body)
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
            relay_matrix_username=relay_matrix_username,
            relay_telegram_username=relay_telegram_username,
            relay_discord_username=relay_discord_username,
        )
