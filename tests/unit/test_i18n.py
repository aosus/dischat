from dischat.i18n import translate


def test_translate_returns_arabic_message() -> None:
    assert "أرسل" in translate("pairing.prompt_code", "ar")


def test_translate_falls_back_to_english() -> None:
    assert translate("pairing.success", "en") == "Pairing complete."
