from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from dischat.commands.parser import parse_command
from dischat.i18n import translate, translate_format
from dischat.pairing.codes import verify_code
from dischat.pairing.service import (
    PairingRateLimitPolicy,
    PairingService,
    evaluate_issuance,
    evaluate_verification,
    is_cooldown_active,
    remaining_seconds,
    should_apply_cooldown,
)
from dischat.storage.repositories import (
    CategoryRecord,
    PairingRateLimitState,
    UserWatchRecord,
)
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


class PairingRateLimitsRepo(Protocol):
    async def get_state(
        self, *, mxid: str, discourse_username: str | None
    ) -> PairingRateLimitState | None: ...

    async def reserve_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        window: timedelta,
        max_issuances: int,
    ) -> datetime | None: ...

    async def record_failure_and_apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        max_failures: int,
        cooldown: timedelta,
    ) -> datetime | None: ...

    async def record_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        now: datetime,
        window: timedelta,
    ) -> PairingRateLimitState: ...

    async def record_failure(
        self, *, mxid: str, discourse_username: str | None, now: datetime
    ) -> PairingRateLimitState: ...

    async def apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        cooldown_until: datetime,
        now: datetime,
        reset_failure_count: bool = False,
    ) -> None: ...


class CategoriesRepo(Protocol):
    async def list_categories(self) -> list[CategoryRecord]: ...

    async def get_by_slug(self, slug: str) -> CategoryRecord | None: ...


class UserWatchesRepo(Protocol):
    async def add_category_watch(self, *, mxid: str, category_id: int) -> UserWatchRecord: ...

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord: ...

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None: ...

    async def remove_watch_all(self, *, mxid: str) -> None: ...

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]: ...


def _rate_limit_now(clock: datetime | None = None) -> datetime:
    """Service-wide rate-limit clock: injectable for tests, wall-clock by default."""
    return clock or datetime.now(UTC)


class DischatService:
    def __init__(
        self,
        *,
        chat_accounts: ChatAccountsRepo,
        pairing_sessions: PairingSessionsRepo,
        categories: CategoriesRepo,
        user_watches: UserWatchesRepo,
        pairing_service: PairingService | None = None,
        pairing_rate_limits: PairingRateLimitsRepo | None = None,
        rate_limit_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_accounts = chat_accounts
        self._pairing_sessions = pairing_sessions
        self._categories = categories
        self._user_watches = user_watches
        self._pairing_service = pairing_service or PairingService()
        self._pairing_rate_limits = pairing_rate_limits
        self._rate_limit_clock = rate_limit_clock

    def _now(self) -> datetime:
        return _rate_limit_now(self._rate_limit_clock() if self._rate_limit_clock else None)

    async def _rate_limit_states(
        self, *, mxid: str, discourse_username: str
    ) -> tuple[PairingRateLimitState | None, PairingRateLimitState | None]:
        """Persistent per-user and per-target rate-limit states (None when disabled)."""
        if self._pairing_rate_limits is None:
            return None, None
        user_state = await self._pairing_rate_limits.get_state(mxid=mxid, discourse_username=None)
        target_state = await self._pairing_rate_limits.get_state(
            mxid=mxid, discourse_username=discourse_username.lower()
        )
        return user_state, target_state

    async def _verification_retry_at(
        self, *, mxid: str, discourse_username: str, now: datetime
    ) -> datetime | None:
        """When verification is blocked, or None.

        Verification is gated only by active cooldowns. The issuance window must
        never block a user from entering a code they were already sent.
        """
        policy = self._rate_limit_policy()
        user_state, target_state = await self._rate_limit_states(
            mxid=mxid, discourse_username=discourse_username
        )
        for state in (user_state, target_state):
            decision = evaluate_verification(state, policy=policy, now=now)
            if not decision.allowed and decision.retry_at is not None:
                return decision.retry_at
        return None

    async def _issuance_retry_at(
        self, *, mxid: str, discourse_username: str, now: datetime
    ) -> datetime | None:
        """When a new pairing code may not be issued, or None.

        Issuance is gated by active cooldowns and the per-window issuance cap.
        """
        policy = self._rate_limit_policy()
        user_state, target_state = await self._rate_limit_states(
            mxid=mxid, discourse_username=discourse_username
        )
        for state in (user_state, target_state):
            if is_cooldown_active(state, now=now):
                assert state is not None and state.cooldown_until is not None
                return state.cooldown_until
        for state in (user_state, target_state):
            decision = evaluate_issuance(state, policy=policy, now=now)
            if not decision.allowed and decision.retry_at is not None:
                return decision.retry_at
        return None

    def _rate_limit_policy(self) -> PairingRateLimitPolicy:
        return self._pairing_service.rate_limit_policy

    async def _record_failure_and_maybe_cooldown(
        self, *, mxid: str, discourse_username: str | None, now: datetime
    ) -> None:
        """Persist a failed verification attempt; enforce cooldown on threshold."""
        if self._pairing_rate_limits is None:
            return
        policy = self._rate_limit_policy()
        atomic = getattr(self._pairing_rate_limits, "record_failure_and_apply_cooldown", None)
        if atomic is not None and discourse_username is not None:
            await atomic(
                mxid=mxid,
                discourse_username=discourse_username,
                now=now,
                max_failures=policy.max_failures,
                cooldown=policy.failure_cooldown,
            )
            return
        target_state = await self._pairing_rate_limits.record_failure(
            mxid=mxid,
            discourse_username=discourse_username.lower() if discourse_username else None,
            now=now,
        )
        user_state = await self._pairing_rate_limits.record_failure(
            mxid=mxid, discourse_username=None, now=now
        )
        for state in (user_state, target_state):
            if not should_apply_cooldown(state, policy=policy, now=now):
                continue
            # The threshold is crossed and any previous cooldown has already
            # expired: (re)arm the cooldown and reset the failure counter so
            # the next cooldown requires a fresh threshold crossing.
            assert state is not None
            cooldown_until = now + policy.failure_cooldown
            await self._pairing_rate_limits.apply_cooldown(
                mxid=mxid,
                discourse_username=state.discourse_username,
                cooldown_until=cooldown_until,
                now=now,
                reset_failure_count=True,
            )
            state.cooldown_until = cooldown_until
            state.failure_count = 0

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
        now = self._now()
        if session.consumed_at is not None or now >= session.expires_at:
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
        retry_at = await self._verification_retry_at(
            mxid=mxid, discourse_username=session.discourse_username, now=now
        )
        if retry_at is not None:
            return ServiceResponse(
                body=translate_format(
                    "pairing.rate_limited",
                    account.response_locale,
                    minutes=str(max(1, remaining_seconds(retry_at, now=now) // 60)),
                )
            )
        updated = await self._pairing_sessions.increment_attempt_count(session.id)
        if updated is None:
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
        code = body.strip()
        if not code.isdigit() or len(code) != 6:
            return ServiceResponse(body=translate("pairing.prompt_code", account.response_locale))
        if updated.attempt_count > 5 or not verify_code(code, updated.code_hash):
            await self._record_failure_and_maybe_cooldown(
                mxid=mxid, discourse_username=updated.discourse_username, now=now
            )
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
        consumed = await self._pairing_sessions.consume_session(updated.id)
        if consumed is None:
            return ServiceResponse(body=translate("pairing.invalid_code", account.response_locale))
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
            requested_username = args[0]
            now = self._now()
            atomic_reserve = (
                getattr(self._pairing_rate_limits, "reserve_issuance", None)
                if self._pairing_rate_limits is not None
                else None
            )
            if atomic_reserve is not None:
                retry_at = await atomic_reserve(
                    mxid=account_mxid,
                    discourse_username=requested_username,
                    now=now,
                    window=self._rate_limit_policy().issuance_window,
                    max_issuances=self._rate_limit_policy().max_issuances_per_window,
                )
            else:
                retry_at = await self._issuance_retry_at(
                    mxid=account_mxid, discourse_username=requested_username, now=now
                )
            if retry_at is not None:
                return ServiceResponse(
                    body=translate_format(
                        "pairing.rate_limited",
                        locale,
                        minutes=str(max(1, remaining_seconds(retry_at, now=now) // 60)),
                    )
                )
            session, raw_code = self._pairing_service.start_session(account_mxid, args[0], now=now)
            if self._pairing_rate_limits is not None and atomic_reserve is None:
                await self._pairing_rate_limits.record_issuance(
                    mxid=account_mxid,
                    discourse_username=requested_username.lower(),
                    now=now,
                    window=self._rate_limit_policy().issuance_window,
                )
                await self._pairing_rate_limits.record_issuance(
                    mxid=account_mxid,
                    discourse_username=None,
                    now=now,
                    window=self._rate_limit_policy().issuance_window,
                )
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
