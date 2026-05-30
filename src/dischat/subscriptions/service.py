from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dischat.subscriptions.categories import Category

WatchMode = Literal["category", "all_public_categories"]


@dataclass(slots=True, frozen=True)
class UserWatch:
    mxid: str
    mode: WatchMode
    category_slug: str | None = None


def is_category_watched(watches: list[UserWatch], category: Category) -> bool:
    for watch in watches:
        if watch.mode == "all_public_categories" and category.is_public:
            return True
        if watch.mode == "category" and watch.category_slug == category.slug:
            return True
    return False
