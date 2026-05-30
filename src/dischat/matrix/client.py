from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nio import (
    AsyncClient,
    InviteMemberEvent,
    LoginError,
    LoginResponse,
    RoomCreateResponse,
    RoomGetEventResponse,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
)

from dischat.matrix.formatting import plain_notice, reply_message


@dataclass(slots=True, frozen=True)
class MatrixSendResult:
    event_id: str
    room_id: str | None = None


@dataclass(slots=True, frozen=True)
class MatrixMessage:
    event_id: str
    room_id: str
    sender: str
    body: str
    parent_event_id: str | None


class MatrixClient(Protocol):
    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult: ...

    async def send_reply(
        self, room_id: str, body: str, parent_event_id: str
    ) -> MatrixSendResult: ...

    async def send_dm(self, mxid: str, body: str) -> MatrixSendResult: ...


class NioMatrixClient:
    def __init__(
        self,
        *,
        homeserver_url: str,
        user_id: str,
        access_token: str | None,
        password: str | None,
    ) -> None:
        self._client = AsyncClient(homeserver_url, user_id)
        if access_token is not None:
            self._client.access_token = access_token
        self._password = password

    @property
    def user_id(self) -> str:
        return self._client.user_id

    async def login(self) -> None:
        if self._client.access_token:
            return
        if not self._password:
            raise ValueError("Matrix password is required when access token is missing.")
        response = await self._client.login(password=self._password)
        if isinstance(response, LoginError):
            raise ValueError(f"Matrix login failed: {response.message}")
        assert isinstance(response, LoginResponse)

    async def close(self) -> None:
        await self._client.close()

    async def sync_once(self, *, since: str | None = None, timeout_ms: int = 0) -> SyncResponse:
        response = await self._client.sync(
            timeout=timeout_ms, since=since, full_state=since is None
        )
        if not isinstance(response, SyncResponse):
            raise ValueError(f"Matrix sync failed: {response}")
        return response

    async def accept_invites(self, sync_response: SyncResponse) -> None:
        for room_id, invite_info in sync_response.rooms.invite.items():
            for event in invite_info.invite_state:
                if isinstance(event, InviteMemberEvent) and event.state_key == self.user_id:
                    await self._client.join(room_id)

    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult:
        response = await self._client.room_send(room_id, "m.room.message", plain_notice(body))
        if not isinstance(response, RoomSendResponse):
            raise ValueError(f"Matrix send_notice failed: {response}")
        return MatrixSendResult(event_id=response.event_id, room_id=room_id)

    async def send_reply(self, room_id: str, body: str, parent_event_id: str) -> MatrixSendResult:
        response = await self._client.room_send(
            room_id,
            "m.room.message",
            reply_message(body, parent_event_id=parent_event_id),
        )
        if not isinstance(response, RoomSendResponse):
            raise ValueError(f"Matrix send_reply failed: {response}")
        return MatrixSendResult(event_id=response.event_id, room_id=room_id)

    async def ensure_dm_room(self, mxid: str) -> str:
        joined = await self._client.joined_rooms()
        if hasattr(joined, "rooms"):
            for room_id in joined.rooms:
                room = self._client.rooms.get(room_id)
                if room is None:
                    continue
                if mxid in room.users and len(room.users) <= 2:
                    return room_id
        response = await self._client.room_create(is_direct=True, invite=[mxid])
        if not isinstance(response, RoomCreateResponse):
            raise ValueError(f"Matrix room_create failed: {response}")
        return response.room_id

    async def send_dm(self, mxid: str, body: str) -> MatrixSendResult:
        room_id = await self.ensure_dm_room(mxid)
        response = await self.send_notice(room_id, body)
        return MatrixSendResult(event_id=response.event_id, room_id=room_id)

    async def get_event(self, *, room_id: str, event_id: str) -> dict[str, Any]:
        response = await self._client.room_get_event(room_id, event_id)
        if not isinstance(response, RoomGetEventResponse):
            raise ValueError(f"Matrix room_get_event failed: {response}")
        return response.event.source

    def extract_messages(self, sync_response: SyncResponse) -> list[MatrixMessage]:
        messages: list[MatrixMessage] = []
        for room_id, room_info in sync_response.rooms.join.items():
            for event in room_info.timeline.events:
                if not isinstance(event, RoomMessageText):
                    continue
                parent_event_id = (
                    event.source.get("content", {})
                    .get("m.relates_to", {})
                    .get("m.in_reply_to", {})
                    .get("event_id")
                )
                messages.append(
                    MatrixMessage(
                        event_id=event.event_id,
                        room_id=room_id,
                        sender=event.sender,
                        body=event.body,
                        parent_event_id=parent_event_id,
                    )
                )
        return messages
