from types import SimpleNamespace
from typing import Any

from dischat.subscriptions.bootstrap import sync_categories_from_discourse


class FakeCategoryRepository:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.disable_calls: list[list[int]] = []

    async def upsert_category(
        self,
        *,
        discourse_category_id: int,
        slug: str,
        name: str,
        is_public: bool,
        enabled: bool = True,
    ) -> Any:
        self.upserts.append(
            {
                "discourse_category_id": discourse_category_id,
                "slug": slug,
                "name": name,
                "is_public": is_public,
                "enabled": enabled,
            }
        )
        return SimpleNamespace(id=len(self.upserts), slug=slug)

    async def disable_categories_not_in(self, discourse_category_ids: list[int]) -> None:
        self.disable_calls.append(discourse_category_ids)


async def test_sync_categories_disables_missing_categories_in_production() -> None:
    repository = FakeCategoryRepository()
    discourse_categories: list[dict[str, object]] = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False},
        {"id": 99, "slug": "private", "name": "Private", "read_restricted": True},
    ]

    await sync_categories_from_discourse(
        categories_repository=repository,
        discourse_categories=discourse_categories,
        live_e2e_category_id=None,
    )

    disable_calls = repository.disable_calls
    assert disable_calls == [[10, 99]]
    private = next(call for call in repository.upserts if call["discourse_category_id"] == 99)
    assert private["is_public"] is False
    assert private["enabled"] is False


async def test_sync_categories_skips_disable_pass_in_live_e2e_mode() -> None:
    repository = FakeCategoryRepository()
    discourse_categories: list[dict[str, object]] = [
        {"id": 56, "slug": "testing", "name": "Testing", "read_restricted": True},
    ]

    await sync_categories_from_discourse(
        categories_repository=repository,
        discourse_categories=discourse_categories,
        live_e2e_category_id=56,
    )

    assert repository.disable_calls == []
    assert len(repository.upserts) == 1
    assert repository.upserts[0]["enabled"] is True
