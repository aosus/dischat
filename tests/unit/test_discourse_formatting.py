from dischat.discourse.formatting import excerpt_text


def test_excerpt_text_collapses_whitespace() -> None:
    assert excerpt_text("hello   world") == "hello world"


def test_excerpt_text_truncates_long_body() -> None:
    result = excerpt_text("a" * 500, limit=20)

    assert len(result) == 20
    assert result.endswith("…")
