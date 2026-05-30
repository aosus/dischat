from __future__ import annotations

from dischat.subscriptions.categories import Category


def sample_categories() -> list[Category]:
    return [
        Category(discourse_category_id=10, slug="support", name="Support", is_public=True),
        Category(
            discourse_category_id=56, slug="dischat-test", name="Dischat Test", is_public=False
        ),
    ]
