from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dischat.config import FileConfig, Settings
from dischat.discourse.client import DiscourseClient
from dischat.matrix.client import NioMatrixClient
from dischat.pairing.service import PairingService
from dischat.service import DischatService
from dischat.storage.db import apply_sql_migrations, create_pool
from dischat.storage.repositories import (
    AuditLogRepository,
    CategoryRepository,
    ChatAccountRepository,
    DeliveryJobRepository,
    DeliveryMessageRepository,
    DiscourseEventRepository,
    PairingSessionRepository,
    RoomLinkRepository,
    UserWatchRepository,
)


@dataclass(slots=True)
class AppContext:
    settings: Settings
    file_config: FileConfig
    discourse_client: DiscourseClient
    matrix_client: NioMatrixClient
    chat_accounts: ChatAccountRepository
    pairing_sessions: PairingSessionRepository
    categories: CategoryRepository
    user_watches: UserWatchRepository
    room_links: RoomLinkRepository
    discourse_events: DiscourseEventRepository
    delivery_jobs: DeliveryJobRepository
    delivery_messages: DeliveryMessageRepository
    audit_logs: AuditLogRepository
    service: DischatService


async def build_context(settings: Settings) -> AppContext:
    pool = await create_pool(settings.database_url)
    await apply_sql_migrations(pool, Path(__file__).resolve().parent / "storage" / "migrations")
    file_config = settings.load_file_config()
    discourse_client = DiscourseClient(
        base_url=settings.discourse_base_url,
        api_key=settings.discourse_api_key,
        api_username=settings.discourse_system_username,
    )
    matrix_client = NioMatrixClient(
        homeserver_url=settings.matrix_homeserver_url,
        user_id=settings.matrix_bot_mxid,
        access_token=settings.matrix_access_token,
        password=settings.matrix_bot_password,
    )
    chat_accounts = ChatAccountRepository(pool)
    pairing_sessions = PairingSessionRepository(pool)
    categories = CategoryRepository(pool)
    user_watches = UserWatchRepository(pool)
    room_links = RoomLinkRepository(pool)
    discourse_events = DiscourseEventRepository(pool)
    delivery_jobs = DeliveryJobRepository(pool)
    delivery_messages = DeliveryMessageRepository(pool)
    audit_logs = AuditLogRepository(pool)
    service = DischatService(
        chat_accounts=chat_accounts,
        pairing_sessions=pairing_sessions,
        categories=categories,
        user_watches=user_watches,
        pairing_service=PairingService(),
    )
    return AppContext(
        settings=settings,
        file_config=file_config,
        discourse_client=discourse_client,
        matrix_client=matrix_client,
        chat_accounts=chat_accounts,
        pairing_sessions=pairing_sessions,
        categories=categories,
        user_watches=user_watches,
        room_links=room_links,
        discourse_events=discourse_events,
        delivery_jobs=delivery_jobs,
        delivery_messages=delivery_messages,
        audit_logs=audit_logs,
        service=service,
    )
