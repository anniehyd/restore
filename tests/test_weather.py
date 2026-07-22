"""Weather + AQI alerts: silence by default, banded dedupe, English voice.

All Open-Meteo calls are stubbed — no live network.
"""

from datetime import datetime

import pytest

import app.main as main
from app import store, weather
from app.weather import ET


MORNING = datetime(2026, 7, 22, 7, 0, tzinfo=ET)
TODAY = "2026-07-22"


@pytest.fixture(autouse=True)
def deterministic_variants(monkeypatch):
    """Always pick the first phrasing variant so assertions are stable."""
    monkeypatch.setattr(weather.random, "choice", lambda seq: seq[0])


def _forecast(overrides=None):
    """Canned Open-Meteo forecast: a benign 75°F day, per-hour overrides.

    overrides: {hour: {"precipitation_probability": 70, "snowfall": 0.5, ...}}
    """
    overrides = overrides or {}
    hours = list(range(24))
    hourly = {
        "time": [f"2026-07-22T{h:02d}:00" for h in hours],
        "temperature_2m": [72.0] * 24,
        "apparent_temperature": [75.0] * 24,
        "precipitation_probability": [0] * 24,
        "precipitation": [0.0] * 24,
        "snowfall": [0.0] * 24,
        "weathercode": [1] * 24,
    }
    for hour, fields in overrides.items():
        for key, value in fields.items():
            hourly[key][hour] = value
    return {"hourly": hourly}


# --- Morning check ------------------------------------------------------------


def test_normal_day_is_silent(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast())
    assert weather.build_morning_alert(MORNING) is None


def test_fetch_failure_is_silent(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: None)
    assert weather.build_morning_alert(MORNING) is None


def test_single_rain_hour(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {15: {"precipitation_probability": 70}}))
    msg = weather.build_morning_alert(MORNING)
    assert msg == ("rain expected around 3pm (70% chance) — "
                   "grab your umbrella before you head out ☂️")


def test_rain_range(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {h: {"precipitation_probability": 60} for h in (14, 15, 16, 17, 18)}))
    msg = weather.build_morning_alert(MORNING)
    assert "from 2pm to 6pm" in msg


def test_rain_below_threshold_is_silent(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {15: {"precipitation_probability": 40}}))
    assert weather.build_morning_alert(MORNING) is None


def test_snow(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {14: {"snowfall": 0.4}}))
    msg = weather.build_morning_alert(MORNING)
    assert "snow expected this afternoon" in msg
    assert "❄️" in msg


def test_extreme_heat(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {14: {"apparent_temperature": 98.0}}))
    msg = weather.build_morning_alert(MORNING)
    assert "feels like 98°F by 2pm" in msg


def test_extreme_cold(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {8: {"apparent_temperature": 12.0}}))
    msg = weather.build_morning_alert(MORNING)
    assert "feels like 12°F" in msg
    assert "🧣" in msg


def test_combined_conditions_are_bubbles(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {15: {"precipitation_probability": 70},
           14: {"apparent_temperature": 97.0}}))
    msg = weather.build_morning_alert(MORNING)
    assert "\n\n" in msg  # separate chat bubbles
    assert "rain" in msg and "heat" in msg


def test_hours_outside_scan_window_ignored(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {6: {"precipitation_probability": 90},   # before 8 AM
           23: {"precipitation_probability": 90}}))  # after 10 PM
    assert weather.build_morning_alert(MORNING) is None


# --- AQI banded dedupe --------------------------------------------------------


def test_good_air_is_silent():
    msg, state = weather.evaluate_aqi(45, {}, TODAY)
    assert msg is None
    assert state["alerted_band"] is None


def test_soft_alert_once_per_day():
    msg1, state = weather.evaluate_aqi(120, {}, TODAY)
    assert "AQI just hit 120" in msg1
    msg2, state = weather.evaluate_aqi(130, state, TODAY)
    assert msg2 is None  # same band, same day


def test_bad_alert_and_no_repeat():
    msg1, state = weather.evaluate_aqi(152, {}, TODAY)
    assert "AQI 152" in msg1 and "stay inside" in msg1
    msg2, state = weather.evaluate_aqi(160, state, TODAY)
    assert msg2 is None  # still bad, no repeat


def test_soft_then_bad_escalates():
    _, state = weather.evaluate_aqi(120, {}, TODAY)
    msg, state = weather.evaluate_aqi(155, state, TODAY)
    assert msg is not None and "155" in msg  # higher band re-alerts


def test_bad_then_soft_does_not_realert():
    _, state = weather.evaluate_aqi(155, {}, TODAY)
    msg, state = weather.evaluate_aqi(120, state, TODAY)
    assert msg is None  # lower band, no alert, no recovery yet


def test_recovery_after_bad():
    _, state = weather.evaluate_aqi(155, {}, TODAY)
    msg, state = weather.evaluate_aqi(60, state, TODAY)
    assert msg == "air's cleared up, safe to head out again 🌿"
    msg2, state = weather.evaluate_aqi(50, state, TODAY)
    assert msg2 is None  # recovery only once


def test_no_recovery_after_soft():
    _, state = weather.evaluate_aqi(120, {}, TODAY)
    msg, _ = weather.evaluate_aqi(60, state, TODAY)
    assert msg is None  # all-clear only follows a "bad" alert


def test_new_day_resets():
    _, state = weather.evaluate_aqi(155, {}, TODAY)
    msg, state = weather.evaluate_aqi(152, state, "2026-07-23")
    assert msg is not None  # fresh day, alert again


# --- get_current_conditions ---------------------------------------------------


def test_current_conditions(monkeypatch):
    now = datetime(2026, 7, 22, 14, 30, tzinfo=ET)
    monkeypatch.setattr(weather, "fetch_forecast", lambda: _forecast(
        {16: {"precipitation_probability": 80},
           14: {"apparent_temperature": 91.0}}))
    monkeypatch.setattr(weather, "fetch_air_quality", lambda: {
        "hourly": {"time": ["2026-07-22T14:00"], "us_aqi": [132.0]}})
    c = weather.get_current_conditions(now)
    assert c == {"aqi": 132, "aqi_band": "soft", "rain_next_3h": True,
                 "snow_next_3h": False, "feels_like_f": 91}


def test_current_conditions_all_fetches_fail(monkeypatch):
    monkeypatch.setattr(weather, "fetch_forecast", lambda: None)
    monkeypatch.setattr(weather, "fetch_air_quality", lambda: None)
    assert weather.get_current_conditions() is None


# --- Scheduler tick -----------------------------------------------------------


def test_morning_tick_sends_once(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_tg_send_chunked", lambda text, markup=None: sent.append(text))
    monkeypatch.setattr(weather, "build_morning_alert", lambda now=None: "rain today ☂️")
    now = datetime(2026, 7, 22, 7, 3, tzinfo=ET)
    main._weather_tick(now)
    main._weather_tick(now)  # second tick same morning: no resend
    assert sent == ["rain today ☂️"]
    assert store.get_weather_state()["morning_date"] == TODAY


def test_morning_tick_silent_day_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_tg_send_chunked", lambda text, markup=None: sent.append(text))
    monkeypatch.setattr(weather, "build_morning_alert", lambda now=None: None)
    main._weather_tick(datetime(2026, 7, 22, 7, 0, tzinfo=ET))
    assert sent == []


def test_aqi_tick_dedupes_run_and_band(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_tg_send_chunked", lambda text, markup=None: sent.append(text))
    monkeypatch.setattr(weather, "fetch_current_aqi", lambda now=None: 152.0)
    ten_am = datetime(2026, 7, 22, 10, 1, tzinfo=ET)
    main._weather_tick(ten_am)
    main._weather_tick(ten_am)  # same run slot: skipped entirely
    main._weather_tick(datetime(2026, 7, 22, 12, 1, tzinfo=ET))  # still bad: no repeat
    assert len(sent) == 1 and "152" in sent[0]


def test_tick_outside_schedule_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(weather, "build_morning_alert", lambda now=None: called.append(1))
    monkeypatch.setattr(weather, "fetch_current_aqi", lambda now=None: called.append(1))
    main._weather_tick(datetime(2026, 7, 22, 9, 30, tzinfo=ET))  # 9 AM: neither slot
    assert called == []
