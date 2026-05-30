from __future__ import annotations


def assert_live_test_category(category_id: int, allowed_category_id: int) -> None:
    if category_id != allowed_category_id:
        raise ValueError(f"Live E2E writes are restricted to category {allowed_category_id}.")
