import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_E2E") != "1",
    reason="Live E2E is disabled unless RUN_LIVE_E2E=1.",
)


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_E2E") == "1",
    reason="Live Matrix and Discourse scenarios are not implemented in this baseline yet.",
)
def test_live_suite_placeholder() -> None:
    pass
