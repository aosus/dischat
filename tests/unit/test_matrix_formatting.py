from typing import cast

from dischat.matrix.formatting import plain_notice, reply_message
from dischat.matrix.replies import get_reply_parent_event_id


def test_plain_notice_uses_notice_type() -> None:
    assert plain_notice("hello")["msgtype"] == "m.notice"


def test_reply_message_embeds_parent_event() -> None:
    payload = reply_message("reply", parent_event_id="$abc")
    relates_to = cast(dict[str, object], payload["m.relates_to"])

    in_reply_to = cast(dict[str, str], relates_to["m.in_reply_to"])
    assert in_reply_to["event_id"] == "$abc"


def test_get_reply_parent_event_id_reads_relation() -> None:
    event = {"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$abc"}}}}

    assert get_reply_parent_event_id(event) == "$abc"
