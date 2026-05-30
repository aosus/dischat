import asyncio

import pytest

from dischat.testing.live import assert_live_test_category, wait_for_non_none


def test_live_safety_allows_expected_category() -> None:
    assert_live_test_category(56, 56)


def test_live_safety_rejects_unexpected_category() -> None:
    with pytest.raises(ValueError):
        assert_live_test_category(12, 56)


async def test_wait_for_non_none_returns_result() -> None:
    attempts = 0

    async def callback() -> str | None:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return None
        return "ready"

    assert await wait_for_non_none(callback, timeout=1.0, interval=0.0) == "ready"


async def test_wait_for_non_none_times_out() -> None:
    async def callback() -> None:
        await asyncio.sleep(0)
        return None

    with pytest.raises(TimeoutError):
        await wait_for_non_none(callback, timeout=0.0, interval=0.0)
