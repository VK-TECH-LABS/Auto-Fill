"""AUTOFILL_SERVICE_TOKEN rotation: the primary or the optional previous token is accepted.

Synthetic tokens only. No candidate data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autofill.service.app import create_app
from autofill.service.auth import (
    AuthConfigurationError,
    accepted_tokens,
    optional_previous_token,
    tokens_match_any,
)

PRIMARY = "synthetic-rotation-primary-0123456789"
PREVIOUS = "synthetic-rotation-previous-9876543210"
OTHER = "synthetic-rotation-other-000000000000"


def _status(client: TestClient, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.get("/v1/sessions/does-not-exist/status", headers=headers).status_code


def test_accepted_tokens_primary_first_and_dedupes():
    assert accepted_tokens(PRIMARY) == (PRIMARY,)
    assert accepted_tokens(f"  {PRIMARY} ", f" {PREVIOUS} ") == (PRIMARY, PREVIOUS)
    assert accepted_tokens(PRIMARY, PRIMARY) == (PRIMARY,)
    assert accepted_tokens(PRIMARY, "") == (PRIMARY,)
    assert accepted_tokens(PRIMARY, None) == (PRIMARY,)


def test_primary_is_still_required_even_with_a_previous_token():
    with pytest.raises(AuthConfigurationError):
        accepted_tokens("", PREVIOUS)
    with pytest.raises(AuthConfigurationError):
        create_app(token="", previous_token=PREVIOUS, run_browser=False)


def test_short_previous_token_is_ignored_not_accepted():
    assert optional_previous_token("short") is None
    assert optional_previous_token("   ") is None
    assert accepted_tokens(PRIMARY, "short") == (PRIMARY,)


def test_tokens_match_any():
    accepted = (PRIMARY, PREVIOUS)
    assert tokens_match_any(PRIMARY, accepted)
    assert tokens_match_any(PREVIOUS, accepted)
    assert not tokens_match_any(OTHER, accepted)
    assert not tokens_match_any(PRIMARY + "x", accepted)
    assert not tokens_match_any("", accepted)
    assert not tokens_match_any(PRIMARY, ())


def test_service_accepts_primary_and_previous_during_rotation():
    app = create_app(token=PRIMARY, previous_token=PREVIOUS, run_browser=False)
    with TestClient(app) as client:
        assert _status(client, PRIMARY) == 404
        assert _status(client, PREVIOUS) == 404
        assert _status(client, OTHER) == 401
        assert _status(client, None) == 401


def test_previous_token_stops_working_once_removed():
    app = create_app(token=PRIMARY, run_browser=False)
    with TestClient(app) as client:
        assert _status(client, PRIMARY) == 404
        assert _status(client, PREVIOUS) == 401


def test_unauthorized_response_does_not_echo_tokens():
    app = create_app(token=PRIMARY, previous_token=PREVIOUS, run_browser=False)
    with TestClient(app) as client:
        response = client.get("/v1/sessions/does-not-exist/status", headers={"Authorization": f"Bearer {OTHER}"})
        assert response.status_code == 401
        for value in (PRIMARY, PREVIOUS, OTHER):
            assert value not in response.text
