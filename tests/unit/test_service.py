from dischat.service import DischatService
from dischat.subscriptions.categories import Category


def test_service_pairing_flow_accepts_plain_code() -> None:
    service = DischatService()

    start = service.handle_message(mxid="@alice:aosus.org", body="/pair test", locale="en")
    assert start is not None
    assert start.pairing_code_to_deliver is not None

    result = service.handle_message(
        mxid="@alice:aosus.org",
        body=start.pairing_code_to_deliver,
        locale="en",
    )

    assert result is not None
    assert result.body == "Pairing complete."


def test_service_prompts_for_code_when_non_digit_text_arrives() -> None:
    service = DischatService()
    service.handle_message(mxid="@alice:aosus.org", body="/pair test", locale="ar")

    result = service.handle_message(mxid="@alice:aosus.org", body="hello", locale="ar")

    assert result is not None
    assert "أرسل رمز الربط" in result.body


def test_service_whoami_after_pairing() -> None:
    service = DischatService()
    start = service.handle_message(mxid="@alice:aosus.org", body="/pair test", locale="en")
    assert start is not None and start.pairing_code_to_deliver is not None
    service.handle_message(mxid="@alice:aosus.org", body=start.pairing_code_to_deliver, locale="en")

    result = service.handle_message(mxid="@alice:aosus.org", body="/whoami", locale="en")

    assert result is not None
    assert result.body == "Paired as test."


def test_service_watch_category_list_respects_live_filter() -> None:
    service = DischatService()
    categories = [
        Category(discourse_category_id=1, slug="support", name="Support", is_public=True),
        Category(
            discourse_category_id=56, slug="dischat-test", name="Dischat Test", is_public=False
        ),
    ]

    result = service.handle_message(
        mxid="@alice:aosus.org",
        body="/watch category",
        locale="en",
        available_categories=categories,
        live_e2e_category_id=56,
    )

    assert result is not None
    assert result.body == "Available categories: dischat-test"
