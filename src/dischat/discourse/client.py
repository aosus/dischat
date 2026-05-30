from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True, frozen=True)
class DiscourseWriteResult:
    post_id: int
    topic_id: int
    raw: str


class DiscourseClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_username: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_username = api_username
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=30.0)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "Api-Username": self._api_username,
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def create_private_message(
        self, *, target_username: str, title: str, raw: str
    ) -> DiscourseWriteResult:
        response = await self._client.post(
            "/posts.json",
            headers=self.headers,
            json={
                "title": title,
                "raw": raw,
                "target_recipients": target_username,
                "archetype": "private_message",
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscourseWriteResult(
            post_id=payload["id"], topic_id=payload["topic_id"], raw=payload["raw"]
        )

    async def create_reply(
        self, *, topic_id: int, raw: str, reply_to_post_number: int | None = None
    ) -> DiscourseWriteResult:
        body: dict[str, Any] = {"topic_id": topic_id, "raw": raw}
        if reply_to_post_number is not None:
            body["reply_to_post_number"] = reply_to_post_number
        response = await self._client.post("/posts.json", headers=self.headers, json=body)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscourseWriteResult(
            post_id=payload["id"], topic_id=payload["topic_id"], raw=payload["raw"]
        )

    async def list_latest_posts(self, *, before: int | None = None) -> list[dict[str, Any]]:
        params = {"before": before} if before is not None else None
        response = await self._client.get("/posts.json", headers=self.headers, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return list(payload.get("latest_posts", []))

    async def get_topic(self, topic_id: int) -> dict[str, Any]:
        response = await self._client.get(f"/t/{topic_id}.json", headers=self.headers)
        response.raise_for_status()
        return dict(response.json())

    async def list_categories(self) -> list[dict[str, Any]]:
        response = await self._client.get("/categories.json", headers=self.headers)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        categories = payload.get("category_list", {}).get("categories", [])
        return list(categories)
