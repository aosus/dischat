from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import httpx


@dataclass(slots=True, frozen=True)
class DiscourseWriteResult:
    post_id: int
    topic_id: int
    raw: str
    post_number: int | None = None


class DiscourseClient:
    _max_retry_delay_seconds = 10.0

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

    def headers_for_user(self, api_username: str | None = None) -> dict[str, str]:
        headers = dict(self.headers)
        if api_username is not None:
            headers["Api-Username"] = api_username
        return headers

    async def _post_with_retry(
        self,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> httpx.Response:
        attempts = 0
        while True:
            response = await self._client.post(path, headers=headers, json=json)
            if response.status_code != 429 or attempts >= 2:
                return response
            delay = self._retry_delay_seconds(response)
            if delay > self._max_retry_delay_seconds:
                return response
            attempts += 1
            await anyio.sleep(delay)

    def _retry_delay_seconds(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        try:
            payload = response.json()
        except ValueError:
            return 5.0
        extras = payload.get("extras", {})
        wait_seconds = extras.get("wait_seconds")
        if isinstance(wait_seconds, int | float):
            return max(float(wait_seconds), 0.0)
        return 5.0

    async def close(self) -> None:
        await self._client.aclose()

    async def create_private_message(
        self,
        *,
        target_username: str,
        title: str,
        raw: str,
        api_username: str | None = None,
    ) -> DiscourseWriteResult:
        response = await self._post_with_retry(
            "/posts.json",
            headers=self.headers_for_user(api_username),
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
            post_id=payload["id"],
            topic_id=payload["topic_id"],
            raw=payload["raw"],
            post_number=payload.get("post_number"),
        )

    async def create_reply(
        self,
        *,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        api_username: str | None = None,
    ) -> DiscourseWriteResult:
        body: dict[str, Any] = {"topic_id": topic_id, "raw": raw}
        if reply_to_post_number is not None:
            body["reply_to_post_number"] = reply_to_post_number
        response = await self._post_with_retry(
            "/posts.json",
            headers=self.headers_for_user(api_username),
            json=body,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscourseWriteResult(
            post_id=payload["id"],
            topic_id=payload["topic_id"],
            raw=payload["raw"],
            post_number=payload.get("post_number"),
        )

    async def create_topic(
        self,
        *,
        title: str,
        raw: str,
        category_id: int,
        api_username: str | None = None,
    ) -> DiscourseWriteResult:
        response = await self._post_with_retry(
            "/posts.json",
            headers=self.headers_for_user(api_username),
            json={
                "title": title,
                "raw": raw,
                "category": category_id,
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscourseWriteResult(
            post_id=payload["id"],
            topic_id=payload["topic_id"],
            raw=payload["raw"],
            post_number=payload.get("post_number"),
        )

    async def list_latest_posts(self, *, before: int | None = None) -> list[dict[str, Any]]:
        params = {"before": before} if before is not None else None
        response = await self._client.get("/posts.json", headers=self.headers, params=params)
        if response.status_code == 403:
            response = await self._client.get("/posts.json", params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return list(payload.get("latest_posts", []))

    async def list_category_latest_posts(
        self, *, category_slug: str, category_id: int
    ) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"/c/{category_slug}/{category_id}/l/latest.json",
            headers=self.headers,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return list(payload.get("topic_list", {}).get("topics", []))

    async def get_topic(self, topic_id: int) -> dict[str, Any]:
        response = await self._client.get(f"/t/{topic_id}.json", headers=self.headers)
        response.raise_for_status()
        return dict(response.json())

    async def get_post(self, post_id: int) -> dict[str, Any]:
        response = await self._client.get(f"/posts/{post_id}.json", headers=self.headers)
        response.raise_for_status()
        return dict(response.json())

    async def list_categories(self) -> list[dict[str, Any]]:
        response = await self._client.get("/categories.json", headers=self.headers)
        if response.status_code == 403:
            # Some Discourse instances reject authenticated category reads while still
            # allowing public category listing and authenticated writes.
            response = await self._client.get("/categories.json")
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        categories = payload.get("category_list", {}).get("categories", [])
        return list(categories)
