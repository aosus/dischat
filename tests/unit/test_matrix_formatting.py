from typing import cast

from dischat.matrix.formatting import plain_notice, plain_text, reply_message, rich_text
from dischat.matrix.replies import get_reply_parent_event_id


def test_plain_text_uses_text_type() -> None:
    payload = plain_text("# Hello\n\nWorld")

    assert payload["msgtype"] == "m.text"
    assert payload["format"] == "org.matrix.custom.html"
    assert payload["formatted_body"] == "<h1>Hello</h1><p>World</p>"


def test_rich_text_uses_provided_html() -> None:
    payload = rich_text("Hello", formatted_body="<p><strong>Hello</strong></p>")

    assert payload["formatted_body"] == "<p><strong>Hello</strong></p>"


def test_plain_notice_uses_notice_type() -> None:
    assert plain_notice("hello")["msgtype"] == "m.notice"


def test_reply_message_embeds_parent_event() -> None:
    payload = reply_message("reply", parent_event_id="$abc", formatted_body="<p>reply</p>")
    relates_to = cast(dict[str, object], payload["m.relates_to"])

    in_reply_to = cast(dict[str, str], relates_to["m.in_reply_to"])
    assert in_reply_to["event_id"] == "$abc"
    assert payload["format"] == "org.matrix.custom.html"
    assert payload["formatted_body"] == "<p>reply</p>"


def test_get_reply_parent_event_id_reads_relation() -> None:
    event = {"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$abc"}}}}

    assert get_reply_parent_event_id(event) == "$abc"
