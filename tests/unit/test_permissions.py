from dischat.security.permissions import can_post_from_chat, detect_platform


def test_detect_platform_infers_matrix() -> None:
    assert detect_platform("@alice:aosus.org") == "matrix"


def test_detect_platform_infers_telegram() -> None:
    assert detect_platform("@telegram_123:aosus.org") == "telegram"


def test_permission_ignores_without_parent_bridge_message() -> None:
    result = can_post_from_chat(
        is_dm=False,
        has_parent_bridge_message=False,
        is_paired=True,
        room_allows_relay=True,
    )

    assert result.decision == "ignore"


def test_permission_requires_pairing_in_dm() -> None:
    result = can_post_from_chat(
        is_dm=True,
        has_parent_bridge_message=True,
        is_paired=False,
        room_allows_relay=False,
    )

    assert result.decision == "reject"
    assert result.reason == "dm_requires_pairing"


def test_permission_allows_relay_in_linked_room() -> None:
    result = can_post_from_chat(
        is_dm=False,
        has_parent_bridge_message=True,
        is_paired=False,
        room_allows_relay=True,
    )

    assert result.decision == "relay"
