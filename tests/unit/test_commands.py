from dischat.commands.parser import ParsedCommand, parse_command


def test_parse_command_returns_none_for_plain_text() -> None:
    assert parse_command("hello") is None


def test_parse_command_parses_args() -> None:
    assert parse_command("/watch category support") == ParsedCommand(
        name="watch",
        args=("category", "support"),
    )
