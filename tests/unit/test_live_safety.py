import pytest

from dischat.testing.live import assert_live_test_category


def test_live_safety_allows_expected_category() -> None:
    assert_live_test_category(56, 56)


def test_live_safety_rejects_unexpected_category() -> None:
    with pytest.raises(ValueError):
        assert_live_test_category(12, 56)
