from dischat.service import DischatService
from dischat.subscriptions.categories import Category


def test_mocked_e2e_pair_watch_and_unwatch_flow() -> None:
    service = DischatService()
    categories = [Category(discourse_category_id=1, slug="support", name="Support", is_public=True)]

    start = service.handle_message(mxid="@alice:aosus.org", body="/pair test", locale="en")
    assert start is not None and start.pairing_code_to_deliver is not None

    paired = service.handle_message(
        mxid="@alice:aosus.org",
        body=start.pairing_code_to_deliver,
        locale="en",
    )
    assert paired is not None
    assert paired.body == "Pairing complete."

    watched = service.handle_message(
        mxid="@alice:aosus.org",
        body="/watch category support",
        locale="en",
        available_categories=categories,
    )
    assert watched is not None
    assert watched.body == "Now watching category support."

    watches = service.handle_message(mxid="@alice:aosus.org", body="/watches", locale="en")
    assert watches is not None
    assert watches.body == "Current watches: support"

    unwatched = service.handle_message(
        mxid="@alice:aosus.org",
        body="/unwatch category support",
        locale="en",
    )
    assert unwatched is not None
    assert unwatched.body == "Stopped watching category support."


def test_mocked_e2e_arabic_pairing_prompt() -> None:
    service = DischatService()

    service.handle_message(mxid="@alice:aosus.org", body="/pair test", locale="ar")
    result = service.handle_message(mxid="@alice:aosus.org", body="abc123", locale="ar")

    assert result is not None
    assert "أرسل رمز الربط" in result.body
