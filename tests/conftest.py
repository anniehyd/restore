"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Point the state store at a fresh per-test file so tests don't share
    conversation history / seen update_ids or pollute the repo with state.json.
    Also keep the weather-alert scheduler from starting under TestClient."""
    monkeypatch.setenv("RESTORE_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("WEATHER_ALERTS_ENABLED", "false")
