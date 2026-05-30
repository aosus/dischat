from dischat.pairing.service import PairingService
from dischat.service import DischatService
from dischat.storage.repositories import (
    CategoryRecord,
    ChatAccount,
    PairingSessionRecord,
    UserWatchRecord,
)


class FakeChatAccounts:
    def __init__(self) -> None:
        self.accounts: dict[str, ChatAccount] = {}

    async def ensure_account(self, *, mxid: str, platform: str, response_locale: str) -> ChatAccount:
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
            'support': CategoryRecord(1, 10, 'support', 'Support', True, True),
            'dischat-test': CategoryRecord(2, 56, 'dischat-test', 'Dischat Test', False, True),
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
        category_slug = 'support' if category_id == 1 else 'dischat-test'
        record = UserWatchRecord(self._next_id, mxid, 'category', category_id, category_slug)
        self._next_id += 1
        self.records.append(record)
        return record

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord:
        record = UserWatchRecord(self._next_id, mxid, 'all_public_categories', None, None)
        self._next_id += 1
        self.records.append(record)
        return record

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None:
        self.records = [record for record in self.records if not (record.mxid == mxid and record.category_id == category_id)]

    async def remove_watch_all(self, *, mxid: str) -> None:
        self.records = [record for record in self.records if not (record.mxid == mxid and record.mode == 'all_public_categories')]

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]:
        return [record for record in self.records if record.mxid == mxid]


def build_service() -> DischatService:
    return DischatService(
        chat_accounts=FakeChatAccounts(),
        pairing_sessions=FakePairingSessions(),
        categories=FakeCategories(),
        user_watches=FakeUserWatches(),
        pairing_service=PairingService(),
    )


async def test_service_pairing_flow_accepts_plain_code() -> None:
    service = build_service()

    start = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='/pair test',
        locale='en',
    )
    assert start is not None
    assert start.pairing_code_to_deliver is not None

    result = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body=start.pairing_code_to_deliver,
        locale='en',
    )

    assert result is not None
    assert result.body == 'Pairing complete.'


async def test_service_prompts_for_code_when_non_digit_text_arrives() -> None:
    service = build_service()
    await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='/pair test',
        locale='ar',
    )

    result = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='hello',
        locale='ar',
    )

    assert result is not None
    assert 'أرسل رمز الربط' in result.body


async def test_service_whoami_after_pairing() -> None:
    service = build_service()
    start = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='/pair test',
        locale='en',
    )
    assert start is not None and start.pairing_code_to_deliver is not None
    await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body=start.pairing_code_to_deliver,
        locale='en',
    )

    result = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='/whoami',
        locale='en',
    )

    assert result is not None
    assert result.body == 'Paired as test.'


async def test_service_watch_category_list_respects_live_filter() -> None:
    service = build_service()

    result = await service.handle_message(
        mxid='@alice:aosus.org',
        platform='matrix',
        body='/watch category',
        locale='en',
        live_e2e_category_id=56,
    )

    assert result is not None
    assert result.body == 'Available categories: dischat-test'
