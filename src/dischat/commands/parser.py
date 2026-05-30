from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...]


def parse_command(body: str) -> ParsedCommand | None:
    stripped = body.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    if not parts:
        return None
    return ParsedCommand(name=parts[0][1:].lower(), args=tuple(parts[1:]))
