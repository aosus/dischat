from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Category:
    discourse_category_id: int
    slug: str
    name: str
    is_public: bool


def filter_watchable_categories(
    categories: list[Category],
    *,
    live_e2e_category_id: int | None = None,
) -> list[Category]:
    if live_e2e_category_id is not None:
        return [
            category
            for category in categories
            if category.discourse_category_id == live_e2e_category_id
        ]
    return [category for category in categories if category.is_public]
