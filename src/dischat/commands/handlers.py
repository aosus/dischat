from __future__ import annotations

from dataclasses import dataclass

from dischat.commands.parser import ParsedCommand
from dischat.config import Locale
from dischat.i18n import translate


@dataclass(slots=True, frozen=True)
class CommandResponse:
    body: str
    handled: bool = True


def handle_unknown_command(command: ParsedCommand, locale: Locale) -> CommandResponse:
    _ = command
    return CommandResponse(body=translate("errors.unknown_command", locale))
