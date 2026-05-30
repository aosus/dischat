from __future__ import annotations

from dischat.config import FileConfig
from dischat.storage.repositories import CategoryRepository, RoomLinkRepository


async def sync_categories_from_discourse(
    *,
    categories_repository: CategoryRepository,
    discourse_categories: list[dict[str, object]],
    live_e2e_category_id: int | None,
) -> dict[str, int]:
    slug_to_id: dict[str, int] = {}
    for raw in discourse_categories:
        discourse_category_id = int(str(raw["id"]))
        is_public = not bool(raw.get("read_restricted", False))
        if live_e2e_category_id is not None and discourse_category_id != live_e2e_category_id:
            continue
        record = await categories_repository.upsert_category(
            discourse_category_id=discourse_category_id,
            slug=str(raw["slug"]),
            name=str(raw["name"]),
            is_public=is_public,
            enabled=is_public or live_e2e_category_id == discourse_category_id,
        )
        slug_to_id[record.slug] = record.id
    return slug_to_id


async def sync_room_links_from_file(
    *,
    room_links_repository: RoomLinkRepository,
    file_config: FileConfig,
    category_lookup: dict[str, int],
) -> None:
    normalized = {
        room_id: {
            "categories": config.categories,
            "allow_relay": config.allow_relay,
            "full_content": config.full_content,
        }
        for room_id, config in file_config.rooms.items()
    }
    await room_links_repository.replace_room_links(normalized, category_lookup)
