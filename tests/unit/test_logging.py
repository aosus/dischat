from __future__ import annotations

import json
import logging

from dischat.logging import JsonFormatter


def test_json_formatter_emits_machine_readable_core_fields() -> None:
    record = logging.LogRecord(
        name="dischat.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="delivery %s failed",
        args=(42,),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "dischat.test"
    assert payload["message"] == "delivery 42 failed"
    assert payload["timestamp"].endswith("+00:00")
