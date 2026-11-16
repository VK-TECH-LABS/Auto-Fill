"""Suite defaults. Visible typing stays instant unless a test opts into the real cadence."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_visible_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFILL_TYPE_DELAY", "0")


@pytest.fixture(autouse=True)
def _reset_committed_country():
    """A United States address committed in one test does not leak into the next."""
    from autofill.mapping import bind_us_page, reset_us_page

    token = bind_us_page()
    yield
    reset_us_page(token)
