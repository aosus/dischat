from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PostingIdentity = Literal["paired", "relay", "reject", "ignore"]


@dataclass(slots=True, frozen=True)
class PostPermission:
    decision: PostingIdentity
    reason: str


def detect_platform(mxid: str) -> str:
    if mxid.startswith("@telegram_"):
        return "telegram"
    if mxid.startswith("@discord_"):
        return "discord"
    return "matrix"


def can_post_from_chat(
    *,
    is_dm: bool,
    has_parent_bridge_message: bool,
    is_paired: bool,
    room_allows_relay: bool,
) -> PostPermission:
    if not has_parent_bridge_message:
        return PostPermission(decision="ignore", reason="missing_parent_bridge_message")
    if is_dm:
        if is_paired:
            return PostPermission(decision="paired", reason="paired_dm_reply")
        return PostPermission(decision="reject", reason="dm_requires_pairing")
    if is_paired:
        return PostPermission(decision="paired", reason="paired_room_reply")
    if room_allows_relay:
        return PostPermission(decision="relay", reason="relay_allowed")
    return PostPermission(decision="reject", reason="room_requires_pairing")
