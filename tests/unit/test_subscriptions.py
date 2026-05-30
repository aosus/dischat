from dischat.subscriptions.categories import Category, filter_watchable_categories
from dischat.subscriptions.service import UserWatch, is_category_watched


def test_filter_watchable_categories_ignores_private_categories_in_production() -> None:
    categories = [
        Category(discourse_category_id=1, slug="public", name="Public", is_public=True),
        Category(discourse_category_id=56, slug="private", name="Private", is_public=False),
    ]

    result = filter_watchable_categories(categories)

    assert [category.slug for category in result] == ["public"]


def test_filter_watchable_categories_only_returns_live_test_category() -> None:
    categories = [
        Category(discourse_category_id=1, slug="public", name="Public", is_public=True),
        Category(discourse_category_id=56, slug="private", name="Private", is_public=False),
    ]

    result = filter_watchable_categories(categories, live_e2e_category_id=56)

    assert [category.slug for category in result] == ["private"]


def test_category_watch_matches_specific_slug() -> None:
    category = Category(discourse_category_id=1, slug="support", name="Support", is_public=True)
    watches = [UserWatch(mxid="@alice:aosus.org", mode="category", category_slug="support")]

    assert is_category_watched(watches, category) is True
