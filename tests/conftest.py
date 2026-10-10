"""Suite defaults. Visible typing stays instant unless a test opts into the real cadence."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_visible_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFILL_TYPE_DELAY", "0")
