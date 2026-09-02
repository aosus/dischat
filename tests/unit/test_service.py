from datetime import UTC, datetime, timedelta

from dischat.pairing.service import PairingRateLimitPolicy, PairingService
from dischat.service import DischatService
from dischat.storage.repositories import (
    CategoryRecord,
    ChatAccount,
    PairingRateLimitState,
    PairingSessionRecord,
    UserWatchRecord,
)


class FakeChatAccounts:
    def __init__(self) -> None:
        self.accounts: dict[str, ChatAccount] = {}

    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount:
        account = self.accounts.get(mxid)
        if account is None:
            account = ChatAccount(
                id=len(self.accounts) + 1,
                mxid=mxid,
                platform=platform,
                discourse_user_id=None,
                discourse_username=None,
                paired_at=None,
                revoked_at=None,
                notify_on_direct_replies=True,
                notify_on_mentions=True,
                response_locale=response_locale,
            )
        else:
            account = ChatAccount(
                id=account.id,
                mxid=account.mxid,
                platform=platform,
                discourse_user_id=account.discourse_user_id,
                discourse_username=account.discourse_username,
                paired_at=account.paired_at,
                revoked_at=account.revoked_at,
                notify_on_direct_replies=account.notify_on_direct_replies,
                notify_on_mentions=account.notify_on_mentions,
                response_locale=response_locale,
            )
        self.accounts[mxid] = account
        return account

    async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
        return self.accounts.get(mxid)

    async def pair_account(
        self,
        *,
        mxid: str,
        discourse_username: str,
        discourse_user_id: int | None = None,
    ) -> ChatAccount:
        account = self.accounts[mxid]
        updated = ChatAccount(
            id=account.id,
            mxid=account.mxid,
            platform=account.platform,
            discourse_user_id=discourse_user_id,
            discourse_username=discourse_username,
            paired_at=account.paired_at,
            revoked_at=None,
            notify_on_direct_replies=account.notify_on_direct_replies,
            notify_on_mentions=account.notify_on_mentions,
            response_locale=account.response_locale,
        )
        self.accounts[mxid] = updated
        return updated

    async def unpair_account(self, *, mxid: str) -> ChatAccount | None:
        account = self.accounts.get(mxid)
        if account is None:
            return None
        updated = ChatAccount(
            id=account.id,
            mxid=account.mxid,
            platform=account.platform,
            discourse_user_id=None,
            discourse_username=None,
            paired_at=account.paired_at,
            revoked_at=account.revoked_at,
            notify_on_direct_replies=account.notify_on_direct_replies,
            notify_on_mentions=account.notify_on_mentions,
            response_locale=account.response_locale,
        )
        self.accounts[mxid] = updated
        return updated


class FakePairingSessions:
    def __init__(self) -> None:
        self.current: dict[str, PairingSessionRecord] = {}
        self._next_id = 1

    async def create_session(
        self,
        *,
        mxid: str,
        discourse_username: str,
        code_hash: str,
        expires_at,
        discourse_user_id=None,
    ) -> PairingSessionRecord:
        record = PairingSessionRecord(
            id=self._next_id,
            mxid=mxid,
            discourse_username=discourse_username,
            discourse_user_id=discourse_user_id,
            code_hash=code_hash,
            expires_at=expires_at,
            consumed_at=None,
            attempt_count=0,
        )
        self._next_id += 1
        self.current[mxid] = record
        return record

    async def get_active_session(self, mxid: str) -> PairingSessionRecord | None:
        return self.current.get(mxid)

    async def increment_attempt_count(self, session_id: int) -> PairingSessionRecord:
        for mxid, record in self.current.items():
            if record.id == session_id:
                updated = PairingSessionRecord(
                    id=record.id,
                    mxid=record.mxid,
                    discourse_username=record.discourse_username,
                    discourse_user_id=record.discourse_user_id,
                    code_hash=record.code_hash,
                    expires_at=record.expires_at,
                    consumed_at=record.consumed_at,
                    attempt_count=record.attempt_count + 1,
                )
                self.current[mxid] = updated
                return updated
        raise KeyError(session_id)

    async def consume_session(self, session_id: int) -> PairingSessionRecord:
        for mxid, record in self.current.items():
            if record.id == session_id:
                updated = PairingSessionRecord(
                    id=record.id,
                    mxid=record.mxid,
                    discourse_username=record.discourse_username,
                    discourse_user_id=record.discourse_user_id,
                    code_hash=record.code_hash,
                    expires_at=record.expires_at,
                    consumed_at=record.expires_at,
                    attempt_count=record.attempt_count,
                )
                self.current[mxid] = updated
                return updated
        raise KeyError(session_id)

    async def cancel_session(self, mxid: str) -> None:
        self.current.pop(mxid, None)


class FakeCategories:
    def __init__(self) -> None:
        self.by_slug: dict[str, CategoryRecord] = {
            "support": CategoryRecord(1, 10, "support", "Support", True, True),
            "dischat-test": CategoryRecord(2, 56, "dischat-test", "Dischat Test", False, True),
        }

    async def list_categories(self) -> list[CategoryRecord]:
        return list(self.by_slug.values())

    async def get_by_slug(self, slug: str) -> CategoryRecord | None:
        return self.by_slug.get(slug)


class FakeUserWatches:
    def __init__(self) -> None:
        self.records: list[UserWatchRecord] = []
        self._next_id = 1

    async def add_category_watch(self, *, mxid: str, category_id: int) -> UserWatchRecord:
        category_slug = "support" if category_id == 1 else "dischat-test"
        record = UserWatchRecord(self._next_id, mxid, "category", category_id, category_slug)
        self._next_id += 1
        self.records.append(record)
        return record

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord:
        record = UserWatchRecord(self._next_id, mxid, "all_public_categories", None, None)
        self._next_id += 1
        self.records.append(record)
        return record

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None:
        self.records = [
            record
            for record in self.records
            if not (record.mxid == mxid and record.category_id == category_id)
        ]

    async def remove_watch_all(self, *, mxid: str) -> None:
        self.records = [
            record
            for record in self.records
            if not (record.mxid == mxid and record.mode == "all_public_categories")
        ]

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]:
        return [record for record in self.records if record.mxid == mxid]


class FakePairingRateLimits:
    """In-memory persistent rate-limit store mirroring migration 0006 semantics."""

    def __init__(self, *, now: datetime) -> None:
        self.now = now
        self._rows: dict[tuple[str, str | None], PairingRateLimitState] = {}

    @staticmethod
    def _key(mxid: str, discourse_username: str | None) -> tuple[str, str | None]:
        return (mxid, discourse_username)

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    @staticmethod
    def _copy(state: PairingRateLimitState) -> PairingRateLimitState:
        return PairingRateLimitState(
            mxid=state.mxid,
            discourse_username=state.discourse_username,
            issuance_count=state.issuance_count,
            failure_count=state.failure_count,
            window_started_at=state.window_started_at,
            cooldown_until=state.cooldown_until,
        )

    async def get_state(
        self, *, mxid: str, discourse_username: str | None
    ) -> PairingRateLimitState | None:
        state = self._rows.get(self._key(mxid, discourse_username))
        if state is None:
            return None
        return self._copy(state)

    async def reserve_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        window: timedelta,
        max_issuances: int,
    ) -> datetime | None:
        target = discourse_username.lower()
        states = [
            self._rows.get(self._key(mxid, None)),
            self._rows.get(self._key(mxid, target)),
        ]
        active_cooldowns = [
            state.cooldown_until
            for state in states
            if state is not None and state.cooldown_until is not None and state.cooldown_until > now
        ]
        if active_cooldowns:
            return max(active_cooldowns)
        for state in states:
            if (
                state is not None
                and now < state.window_started_at + window
                and state.issuance_count >= max_issuances
            ):
                return state.window_started_at + window
        for username in (None, target):
            await self.record_issuance(
                mxid=mxid,
                discourse_username=username,
                now=now,
                window=window,
            )
        return None

    async def record_failure_and_apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        max_failures: int,
        cooldown: timedelta,
    ) -> datetime | None:
        armed_until: datetime | None = None
        for username in (None, discourse_username.lower()):
            state = self._rows.get(self._key(mxid, username))
            if state is None:
                state = PairingRateLimitState(
                    mxid=mxid,
                    discourse_username=username,
                    issuance_count=0,
                    failure_count=0,
                    window_started_at=now,
                    cooldown_until=None,
                )
                self._rows[self._key(mxid, username)] = state
            if state.cooldown_until is not None and state.cooldown_until <= now:
                state.failure_count = 0
                state.cooldown_until = None
            state.failure_count += 1
            if state.failure_count >= max_failures and not (
                state.cooldown_until is not None and state.cooldown_until > now
            ):
                state.failure_count = 0
                state.cooldown_until = now + cooldown
                armed_until = max(armed_until or state.cooldown_until, state.cooldown_until)
        return armed_until

    async def record_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        now: datetime,
        window: timedelta,
    ) -> PairingRateLimitState:
        state = self._rows.get(self._key(mxid, discourse_username))
        if state is not None and now >= state.window_started_at + window:
            state.window_started_at = now
            state.issuance_count = 0
        if state is None:
            state = PairingRateLimitState(
                mxid=mxid,
                discourse_username=discourse_username,
                issuance_count=0,
                failure_count=0,
                window_started_at=now,
                cooldown_until=None,
            )
            self._rows[self._key(mxid, discourse_username)] = state
        state.issuance_count += 1
        return self._copy(state)

    async def record_failure(
        self, *, mxid: str, discourse_username: str | None, now: datetime
    ) -> PairingRateLimitState:
        state = self._rows.get(self._key(mxid, discourse_username))
        if state is None:
            state = PairingRateLimitState(
                mxid=mxid,
                discourse_username=discourse_username,
                issuance_count=0,
                failure_count=0,
                window_started_at=now,
                cooldown_until=None,
            )
            self._rows[self._key(mxid, discourse_username)] = state
        state.failure_count += 1
        return self._copy(state)

    async def apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        cooldown_until: datetime,
        now: datetime,
        reset_failure_count: bool = False,
    ) -> None:
        state = self._rows.get(self._key(mxid, discourse_username))
        assert state is not None
        state.cooldown_until = cooldown_until
        if reset_failure_count:
            state.failure_count = 0


def build_service() -> DischatService:
    return DischatService(
        chat_accounts=FakeChatAccounts(),
        pairing_sessions=FakePairingSessions(),
        categories=FakeCategories(),
        user_watches=FakeUserWatches(),
        pairing_service=PairingService(),
    )


def build_rate_limited_service(now: datetime) -> tuple[DischatService, FakePairingRateLimits]:
    rate_limits = FakePairingRateLimits(now=now)
    service = DischatService(
        chat_accounts=FakeChatAccounts(),
        pairing_sessions=FakePairingSessions(),
        categories=FakeCategories(),
        user_watches=FakeUserWatches(),
        pairing_service=PairingService(rate_limit_policy=PairingRateLimitPolicy()),
        pairing_rate_limits=rate_limits,
        rate_limit_clock=lambda: rate_limits.now,
    )
    return service, rate_limits


async def test_service_pairing_flow_accepts_plain_code() -> None:
    service = build_service()

    start = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/pair test",
        locale="en",
    )
    assert start is not None
    assert start.pairing_code_to_deliver is not None

    result = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body=start.pairing_code_to_deliver,
        locale="en",
    )

    assert result is not None
    assert result.body == "Pairing complete."


async def test_service_prompts_for_code_when_non_digit_text_arrives() -> None:
    service = build_service()
    await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/pair test",
        locale="ar",
    )

    result = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="hello",
        locale="ar",
    )

    assert result is not None
    assert "أرسل رمز الربط" in result.body


async def test_service_whoami_after_pairing() -> None:
    service = build_service()
    start = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/pair test",
        locale="en",
    )
    assert start is not None and start.pairing_code_to_deliver is not None
    await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body=start.pairing_code_to_deliver,
        locale="en",
    )

    result = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/whoami",
        locale="en",
    )

    assert result is not None
    assert result.body == "Paired as test."


async def test_service_watch_category_list_respects_live_filter() -> None:
    service = build_service()

    result = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/watch category",
        locale="en",
        live_e2e_category_id=56,
    )

    assert result is not None
    assert result.body == "Available categories: dischat-test"


# --- Persistent pairing rate-limit / abuse-protection flows (issue #14) ---


ISSUANCE_LIMIT = PairingRateLimitPolicy().max_issuances_per_window
FAILURE_LIMIT = PairingRateLimitPolicy().max_failures
COOLDOWN = PairingRateLimitPolicy().failure_cooldown


async def test_repeated_pair_cannot_reset_issuance_limit() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, _ = build_rate_limited_service(now)

    for _ in range(ISSUANCE_LIMIT):
        start = await service.handle_message(
            mxid="@alice:aosus.org",
            platform="matrix",
            body="/pair bob",
            locale="en",
        )
        assert start is not None and start.pairing_code_to_deliver is not None

    blocked = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/pair bob",
        locale="en",
    )

    assert blocked is not None
    assert "Too many pairing attempts" in blocked.body
    assert blocked.pairing_code_to_deliver is None


async def test_rate_limited_pair_issues_no_discourse_pm_target() -> None:
    """A blocked /pair must not produce a code/PM target for the handler to send."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, _ = build_rate_limited_service(now)

    for _ in range(ISSUANCE_LIMIT):
        await service.handle_message(
            mxid="@alice:aosus.org",
            platform="matrix",
            body="/pair target",
            locale="en",
        )

    blocked = await service.handle_message(
        mxid="@alice:aosus.org",
        platform="matrix",
        body="/pair someone_else",
        locale="en",
    )

    assert blocked is not None
    assert blocked.pairing_code_to_deliver is None
    assert blocked.pairing_target_username is None


async def test_new_session_after_failures_still_cooldown_blocked() -> None:
    """Failed-verification cooldown persists even when a new session replaces the old."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, rate_limits = build_rate_limited_service(now)

    start = await service.handle_message(
        mxid="@alice:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    assert start is not None and start.pairing_code_to_deliver is not None
    for _ in range(FAILURE_LIMIT):
        result = await service.handle_message(
            mxid="@alice:aosus.org", platform="matrix", body="000000", locale="en"
        )
        assert result is not None

    # Cooldown is active; a brand-new pairing session must not reset it.
    limited = await service.handle_message(
        mxid="@alice:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    assert limited is not None
    assert "Too many pairing attempts" in limited.body
    assert limited.pairing_code_to_deliver is None

    rate_limits.advance(COOLDOWN + timedelta(minutes=1))
    recovered = await service.handle_message(
        mxid="@alice:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    assert recovered is not None
    assert recovered.pairing_code_to_deliver is not None


async def test_failed_verification_cooldown_persists_across_new_sessions() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, rate_limits = build_rate_limited_service(now)

    start = await service.handle_message(
        mxid="@carol:aosus.org", platform="matrix", body="/pair dave", locale="en"
    )
    assert start is not None and start.pairing_code_to_deliver is not None
    for _ in range(FAILURE_LIMIT):
        bad = await service.handle_message(
            mxid="@carol:aosus.org", platform="matrix", body="999999", locale="en"
        )
        assert bad is not None
        assert "invalid" in bad.body or "غير صالح" in bad.body

    cooldown_state = await rate_limits.get_state(mxid="@carol:aosus.org", discourse_username=None)
    assert cooldown_state is not None
    assert cooldown_state.cooldown_until is not None

    # Starting yet another session does not clear the cooldown for verification.
    await service.handle_message(
        mxid="@carol:aosus.org", platform="matrix", body="/pair dave", locale="en"
    )
    verify_attempt = await service.handle_message(
        mxid="@carol:aosus.org", platform="matrix", body="123456", locale="en"
    )
    assert verify_attempt is not None
    assert "Too many pairing attempts" in verify_attempt.body

    rate_limits.advance(COOLDOWN + timedelta(seconds=1))
    recovered = await service.handle_message(
        mxid="@carol:aosus.org", platform="matrix", body="/pair dave", locale="en"
    )
    assert recovered is not None
    assert recovered.pairing_code_to_deliver is not None


async def test_verification_succeeds_after_max_issuances() -> None:
    """Exhausting the issuance window must not block verifying the last issued code."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, rate_limits = build_rate_limited_service(now)

    last_code: str | None = None
    for _ in range(ISSUANCE_LIMIT):
        start = await service.handle_message(
            mxid="@erin:aosus.org", platform="matrix", body="/pair bob", locale="en"
        )
        assert start is not None and start.pairing_code_to_deliver is not None
        last_code = start.pairing_code_to_deliver
    assert last_code is not None

    user_state = await rate_limits.get_state(mxid="@erin:aosus.org", discourse_username=None)
    assert user_state is not None
    assert user_state.issuance_count == ISSUANCE_LIMIT

    # The most recently issued code verifies normally even though the
    # issuance window is exhausted (only cooldowns gate verification).
    result = await service.handle_message(
        mxid="@erin:aosus.org",
        platform="matrix",
        body=last_code,
        locale="en",
    )
    assert result is not None
    assert result.body == "Pairing complete."


async def test_cooldown_rearms_after_first_cooldown_expires() -> None:
    """A second threshold crossing after the first cooldown must apply a new cooldown."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    service, rate_limits = build_rate_limited_service(now)

    # First cooldown cycle.
    start = await service.handle_message(
        mxid="@frank:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    assert start is not None and start.pairing_code_to_deliver is not None
    for _ in range(FAILURE_LIMIT):
        await service.handle_message(
            mxid="@frank:aosus.org", platform="matrix", body="000000", locale="en"
        )
    first = await rate_limits.get_state(mxid="@frank:aosus.org", discourse_username=None)
    assert first is not None
    assert first.cooldown_until is not None
    assert first.cooldown_until > rate_limits.now

    # Let the first cooldown expire.
    rate_limits.advance(COOLDOWN + timedelta(seconds=1))

    # Failures after expiry must be able to trigger a NEW cooldown.
    await service.handle_message(
        mxid="@frank:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    for _ in range(FAILURE_LIMIT):
        await service.handle_message(
            mxid="@frank:aosus.org", platform="matrix", body="000000", locale="en"
        )
    second = await rate_limits.get_state(mxid="@frank:aosus.org", discourse_username=None)
    assert second is not None
    assert second.cooldown_until is not None
    assert second.cooldown_until > rate_limits.now, (
        "no new cooldown was armed after the first one expired"
    )
    # Failure counter was reset when the second cooldown was armed.
    assert second.failure_count == 0

    # The re-armed cooldown blocks verification too.
    await service.handle_message(
        mxid="@frank:aosus.org", platform="matrix", body="/pair bob", locale="en"
    )
    blocked = await service.handle_message(
        mxid="@frank:aosus.org", platform="matrix", body="111111", locale="en"
    )
    assert blocked is not None
    assert "Too many pairing attempts" in blocked.body
