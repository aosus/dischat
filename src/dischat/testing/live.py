from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import TypeVar

T = TypeVar("T")


def assert_live_test_category(category_id: int, allowed_category_id: int) -> None:
    if category_id != allowed_category_id:
        raise ValueError(f"Live E2E writes are restricted to category {allowed_category_id}.")


def require_live_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required live E2E environment variable: {name}")
    return value


async def wait_for_non_none(
    callback: Callable[[], Awaitable[T | None]],
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
) -> T:
    deadline = monotonic() + timeout
    while True:
        result = await callback()
        if result is not None:
            return result
        if monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for live E2E condition.")
        await asyncio.sleep(interval)
