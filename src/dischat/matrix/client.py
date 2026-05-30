from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True, frozen=True)
class MatrixSendResult:
    event_id: str


class MatrixClient(Protocol):
    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult: ...

    async def send_reply(
        self, room_id: str, body: str, parent_event_id: str
    ) -> MatrixSendResult: ...

    async def send_dm(self, mxid: str, body: str) -> MatrixSendResult: ...
