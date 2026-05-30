from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from dischat.commands.parser import parse_command
from dischat.i18n import translate, translate_format
from dischat.pairing.codes import verify_code
from dischat.pairing.service import PairingService
from dischat.storage.repositories import CategoryRecord, UserWatchRecord
from dischat.subscriptions.categories import Category, filter_watchable_categories


@dataclass(slots=True, frozen=True)
class ServiceResponse:
    body: str
    pairing_code_to_deliver: str | None = None
    pairing_target_username: str | None = None


class ChatAccountsRepo(Protocol):
    async def ensure_account(self, *, mxid: str, platform: str, response_locale: str): ...

    async def get_by_mxid(self, mxid: str): ...

    async def pair_account(
        self,
        *,
        mxid: str,
        discourse_username: str,
        discourse_user_id: int | None = None,
    ): ...

    async def unpair_account(self, *, mxid: str): ...


class PairingSessionsRepo(Protocol):
    async def create_session(
        self,
        *,
        mxid: str,
        discourse_username: str,
        code_hash: str,
        expires_at: datetime,
        discourse_user_id: int | None = None,
    ): ...

    async def get_active_session(self, mxid: str): ...

    async def increment_attempt_count(self, session_id: int): ...

    async def consume_session(self, session_id: int): ...

    async def cancel_session(self, mxid: str) -> None: ...


class CategoriesRepo(Protocol):
    async def list_categories(self) -> list[CategoryRecord]: ...

    async def get_by_slug(self, slug: str) -> CategoryRecord | None: ...


class UserWatchesRepo(Protocol):
    async def add_category_watch(self, *, mxid: str, category_id: int) -> UserWatchRecord: ...

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord: ...

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None: ...

    async def remove_watch_all(self, *, mxid: str) -> None: ...

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]: ...


class DischatService:
    def __init__(
        self,
        *,
        chat_accounts: ChatAccountsRepo,
        pairing_sessions: PairingSessionsRepo,
        categories: CategoriesRepo,
        user_watches: UserWatchesRepo,
        pairing_service: PairingService | None = None,
    ) -> None:
        self._chat_accounts = chat_accounts
        self._pairing_sessions = pairing_sessions
        self._categories = categories
        self._user_watches = user_watches
        self._pairing_service = pairing_service or PairingService()

    async def handle_message(
        self,
        *,
        mxid: str,
        platform: str,
        body: str,
        locale: str,
        live_e2e_category_id: int | None = None,
    ) -> ServiceResponse | None:
        account = await self._chat_accounts.ensure_account(
            mxid=mxid,
            platform=platform,
            response_locale=locale,
        )
        command = parse_command(body)
        if command is not None:
            return await self._handle_command(
                account_mxid=mxid,
                locale=account.response_locale,
                command_name=command.name,
                args=command.args,
                live_e2e_category_id=live_e2e_category_id,
            )
        session = await self._pairing_sessions.get_active_session(mxid)
        if session is None:
            return None
        now = datetime.now(UTC)
        if session.consumed_at is not None or now >= session.expires_at:
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
        updated = await self._pairing_sessions.increment_attempt_count(session.id)
        code = body.strip()
        if not code.isdigit() or len(code) != 6:
            return ServiceResponse(body=translate("pairing.prompt_code", account.response_locale))
        if updated.attempt_count > 5 or not verify_code(code, updated.code_hash):
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
        await self._pairing_sessions.consume_session(updated.id)
        await self._chat_accounts.pair_account(
            mxid=mxid,
            discourse_username=updated.discourse_username,
            discourse_user_id=updated.discourse_user_id,
        )
        return ServiceResponse(body=translate("pairing.success", account.response_locale))

    async def _handle_command(
        self,
        *,
        account_mxid: str,
        locale: str,
        command_name: str,
        args: tuple[str, ...],
        live_e2e_category_id: int | None,
    ) -> ServiceResponse:
        if command_name == "pair" and len(args) == 1:
            session, raw_code = self._pairing_service.start_session(account_mxid, args[0])
            await self._pairing_sessions.create_session(
                mxid=session.mxid,
                discourse_username=session.discourse_username,
                discourse_user_id=None,
                code_hash=session.code_hash,
                expires_at=session.expires_at,
            )
            return ServiceResponse(
                body=translate("pairing.code_sent", locale),
                pairing_code_to_deliver=raw_code,
                pairing_target_username=args[0],
            )
        if command_name == "cancel":
            await self._pairing_sessions.cancel_session(account_mxid)
            return ServiceResponse(body=translate("pairing.cancelled", locale))
        if command_name == "whoami":
            account = await self._chat_accounts.get_by_mxid(account_mxid)
            if (
                account is None
                or account.discourse_username is None
                or account.revoked_at is not None
            ):
                return ServiceResponse(body=translate("pairing.unpaired", locale))
            return ServiceResponse(
                body=translate_format("pairing.whoami", locale, username=account.discourse_username)
            )
        if command_name == "unpair":
            await self._pairing_sessions.cancel_session(account_mxid)
            await self._chat_accounts.unpair_account(mxid=account_mxid)
            return ServiceResponse(body=translate("pairing.unpaired_success", locale))

        categories = await self._categories.list_categories()
        watchable_categories = filter_watchable_categories(
            [
                Category(
                    discourse_category_id=category.discourse_category_id,
                    slug=category.slug,
                    name=category.name,
                    is_public=category.is_public,
                )
                for category in categories
            ],
            live_e2e_category_id=live_e2e_category_id,
        )

        if command_name == "watch" and args == ("category",):
            if not watchable_categories:
                return ServiceResponse(body=translate("watch.category_list_empty", locale))
            names = ", ".join(category.slug for category in watchable_categories)
            return ServiceResponse(
                body=translate_format("watch.category_list", locale, categories=names)
            )
        if command_name == "watch" and len(args) == 2 and args[0] == "category":
            category = await self._categories.get_by_slug(args[1])
            allowed_slugs = {item.slug for item in watchable_categories}
            if category is None or category.slug not in allowed_slugs:
                return ServiceResponse(body=translate("watch.unknown_category", locale))
            await self._user_watches.add_category_watch(mxid=account_mxid, category_id=category.id)
            return ServiceResponse(body=translate_format("watch.added", locale, slug=category.slug))
        if command_name == "watch" and args == ("all",):
            await self._user_watches.add_watch_all(mxid=account_mxid)
            return ServiceResponse(body=translate("watch.all_added", locale))
        if command_name == "unwatch" and len(args) == 2 and args[0] == "category":
            category = await self._categories.get_by_slug(args[1])
            if category is not None:
                await self._user_watches.remove_category_watch(
                    mxid=account_mxid, category_id=category.id
                )
            return ServiceResponse(body=translate_format("watch.removed", locale, slug=args[1]))
        if command_name == "unwatch" and args == ("all",):
            await self._user_watches.remove_watch_all(mxid=account_mxid)
            return ServiceResponse(body=translate("watch.all_removed", locale))
        if command_name == "watches":
            watches = await self._user_watches.list_watches_for_mxid(account_mxid)
            entries = ["all" for watch in watches if watch.mode == "all_public_categories"]
            entries.extend(
                watch.category_slug for watch in watches if watch.category_slug is not None
            )
            if not entries:
                return ServiceResponse(body=translate("watch.none", locale))
            return ServiceResponse(
                body=translate_format("watch.current", locale, watches=", ".join(sorted(entries)))
            )
        return ServiceResponse(body=translate("errors.unknown_command", locale))


def backoff_delay(attempts: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=min(300, 2**attempts))
