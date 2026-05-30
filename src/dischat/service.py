from __future__ import annotations

from dataclasses import dataclass, field

from dischat.commands.parser import parse_command
from dischat.i18n import translate, translate_format
from dischat.pairing.service import PairingService, PairingSession
from dischat.subscriptions.categories import Category, filter_watchable_categories


@dataclass(slots=True, frozen=True)
class ServiceResponse:
    body: str
    pairing_code_to_deliver: str | None = None


@dataclass(slots=True)
class UserState:
    locale: str
    discourse_username: str | None = None
    active_pairing: PairingSession | None = None
    category_watches: set[str] = field(default_factory=set)
    watch_all: bool = False


class DischatService:
    def __init__(self, pairing_service: PairingService | None = None) -> None:
        self._pairing_service = pairing_service or PairingService()
        self._users: dict[str, UserState] = {}

    def _state_for(self, mxid: str, locale: str) -> UserState:
        state = self._users.get(mxid)
        if state is None:
            state = UserState(locale=locale)
            self._users[mxid] = state
        else:
            state.locale = locale
        return state

    def handle_message(
        self,
        *,
        mxid: str,
        body: str,
        locale: str,
        available_categories: list[Category] | None = None,
        live_e2e_category_id: int | None = None,
    ) -> ServiceResponse | None:
        state = self._state_for(mxid, locale)
        command = parse_command(body)
        if command is not None:
            return self._handle_command(
                mxid=mxid,
                state=state,
                command_name=command.name,
                args=command.args,
                available_categories=available_categories or [],
                live_e2e_category_id=live_e2e_category_id,
            )
        if state.active_pairing is None:
            return None
        result = self._pairing_service.validate_code(state.active_pairing, body.strip())
        if result.ok:
            state.discourse_username = state.active_pairing.discourse_username
            state.active_pairing = None
        return ServiceResponse(body=self._pairing_service.render_message(result, state.locale))

    def _handle_command(
        self,
        *,
        mxid: str,
        state: UserState,
        command_name: str,
        args: tuple[str, ...],
        available_categories: list[Category],
        live_e2e_category_id: int | None,
    ) -> ServiceResponse:
        if command_name == "pair" and len(args) == 1:
            session, raw_code = self._pairing_service.start_session(mxid, args[0])
            state.active_pairing = session
            return ServiceResponse(
                body=translate("pairing.code_sent", state.locale),
                pairing_code_to_deliver=raw_code,
            )
        if command_name == "cancel":
            state.active_pairing = None
            return ServiceResponse(body=translate("pairing.cancelled", state.locale))
        if command_name == "whoami":
            if state.discourse_username is None:
                return ServiceResponse(body=translate("pairing.unpaired", state.locale))
            return ServiceResponse(
                body=translate_format(
                    "pairing.whoami", state.locale, username=state.discourse_username
                )
            )
        if command_name == "unpair":
            state.active_pairing = None
            state.discourse_username = None
            return ServiceResponse(body=translate("pairing.unpaired_success", state.locale))
        if command_name == "watch" and args == ("category",):
            watchable = filter_watchable_categories(
                available_categories,
                live_e2e_category_id=live_e2e_category_id,
            )
            if not watchable:
                return ServiceResponse(body=translate("watch.category_list_empty", state.locale))
            names = ", ".join(category.slug for category in watchable)
            return ServiceResponse(
                body=translate_format("watch.category_list", state.locale, categories=names)
            )
        if command_name == "watch" and len(args) == 2 and args[0] == "category":
            slug = args[1]
            watchable = filter_watchable_categories(
                available_categories,
                live_e2e_category_id=live_e2e_category_id,
            )
            if slug not in {category.slug for category in watchable}:
                return ServiceResponse(body=translate("watch.unknown_category", state.locale))
            state.category_watches.add(slug)
            return ServiceResponse(body=translate_format("watch.added", state.locale, slug=slug))
        if command_name == "watch" and args == ("all",):
            state.watch_all = True
            return ServiceResponse(body=translate("watch.all_added", state.locale))
        if command_name == "unwatch" and len(args) == 2 and args[0] == "category":
            slug = args[1]
            state.category_watches.discard(slug)
            return ServiceResponse(body=translate_format("watch.removed", state.locale, slug=slug))
        if command_name == "unwatch" and args == ("all",):
            state.watch_all = False
            return ServiceResponse(body=translate("watch.all_removed", state.locale))
        if command_name == "watches":
            watches = sorted(state.category_watches)
            if state.watch_all:
                watches = ["all"] + watches
            if not watches:
                return ServiceResponse(body=translate("watch.none", state.locale))
            return ServiceResponse(
                body=translate_format("watch.current", state.locale, watches=", ".join(watches))
            )
        return ServiceResponse(body=translate("errors.unknown_command", state.locale))
