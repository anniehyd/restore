"""Weather + air quality alerts via Open-Meteo (no API key).

SILENCE BY DEFAULT: normal weather produces no message at all. AnAn only
speaks up for an actionable special condition — rain, snow, bad air, extreme
heat or cold. Never a "today looks fine" summary.

Two entry points drive the scheduler in main.py:
  build_morning_alert()  -> one combined message for today (or None = silence)
  evaluate_aqi()         -> pure banded-dedupe logic for the 2-hourly AQI check

get_current_conditions() feeds the advisor so outdoor suggestions (walks,
errands, restore blocks) bend around rain/AQI/extreme temps.

All fetches fail soft: log, one retry with a 5s backoff, then give up quietly.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# --- Location + thresholds: the single configuration point (env overrides) ---
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "40.7128"))    # NYC
WEATHER_LON = float(os.environ.get("WEATHER_LON", "-74.0060"))
RAIN_PROB_THRESHOLD = int(os.environ.get("RAIN_PROB_THRESHOLD", "50"))  # %
AQI_ALERT = int(os.environ.get("AQI_ALERT", "150"))  # us_aqi >= this: strong alert
AQI_SOFT = int(os.environ.get("AQI_SOFT", "100"))    # us_aqi > this: soft alert
HEAT_F = int(os.environ.get("HEAT_F", "95"))         # apparent temp >= this
COLD_F = int(os.environ.get("COLD_F", "20"))         # apparent temp <= this

MORNING_SCAN_HOURS = range(8, 23)  # today 8 AM - 10 PM

# --- Message variants (English only; AnAn's voice) -----------------------

RAIN_SINGLE = [
    "rain expected around {t} ({p}% chance) — grab your umbrella before you head out ☂️",
    "looks like rain around {t} ({p}% chance) — umbrella time ☂️",
    "heads up 🧡 rain around {t} ({p}% chance) — keep an umbrella close",
]
RAIN_RANGE = [
    "rain from {a} to {b}, plan around it ☂️",
    "wet stretch from {a} to {b} — time your errands around it ☂️",
    "on-and-off rain from {a} to {b} — umbrella's your friend today 🧡",
]
SNOW = [
    "snow expected this {part} — bundle up and watch your step ❄️",
    "snow on the way this {part} — cozy layers and careful steps ❄️",
    "it's going to snow this {part} — wrap up warm 🧡❄️",
]
HEAT = [
    "heat warning today, feels like {t}°F by {h} — stay hydrated and avoid the midday sun 🥵",
    "it's a scorcher — feels like {t}°F around {h}. water bottle, shade, easy pace 🥵",
    "serious heat today, {t}°F feels-like by {h} — take it slow and drink up 🧡",
]
COLD = [
    "brutal cold today, feels like {t}°F — layer up if you go out 🧣",
    "it's bitterly cold out there, feels like {t}°F — bundle up properly 🧣",
    "feels like {t}°F today 🥶 — big coat weather, layer up 🧡",
]
AQI_BAD = [
    "air quality is really bad right now (AQI {aqi}) — stay inside and keep the windows closed 😷",
    "the air's rough out there (AQI {aqi}) — best to stay in and close the windows 😷",
    "AQI just hit {aqi} 😷 — indoor day, keep the windows shut 🧡",
]
AQI_SOFT_MSG = [
    "AQI just hit {aqi} — maybe wear a mask if you're heading out",
    "air's a little off today (AQI {aqi}) — a mask wouldn't hurt if you go out",
    "AQI's at {aqi} right now — nothing scary, but a mask outside is a good idea 🧡",
]
AQI_RECOVERY = [
    "air's cleared up, safe to head out again 🌿",
    "good news — the air's back to normal, you're free to go out 🌿",
    "AQI's dropped back down 🌿 outside is yours again 🧡",
]


def _pick(variants: list[str], **kwargs) -> str:
    return random.choice(variants).format(**kwargs)


def _clock(hour: int) -> str:
    """3pm-style label from a 24h hour."""
    return datetime(2000, 1, 1, hour).strftime("%I%p").lstrip("0").lower()


def _part_of_day(hour: int) -> str:
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


# --- Fetching (fail soft, one retry with 5s backoff) ---------------------


def _get(url: str, params: dict) -> Optional[dict]:
    for attempt in (1, 2):
        try:
            resp = httpx.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — fail silently, never alert the user
            log.warning("Open-Meteo fetch failed (attempt %d/2, %s): %s", attempt, url, exc)
            if attempt == 1:
                time.sleep(5)
    return None


def fetch_forecast() -> Optional[dict]:
    return _get(FORECAST_URL, {
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,"
                  "precipitation,snowfall,weathercode",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
        "forecast_days": 2,
    })


def fetch_air_quality() -> Optional[dict]:
    return _get(AIR_QUALITY_URL, {
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "hourly": "us_aqi",
        "timezone": "America/New_York",
        "forecast_days": 2,
    })


def _hourly_rows(data: dict) -> list[dict]:
    """Zip Open-Meteo's parallel hourly arrays into row dicts with a datetime."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    keys = [k for k in hourly if k != "time"]
    rows = []
    for i, t in enumerate(times):
        try:
            row = {"dt": datetime.fromisoformat(t).replace(tzinfo=ET)}
        except ValueError:
            continue
        for k in keys:
            values = hourly.get(k) or []
            row[k] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


# --- Morning check (7 AM: scan today 8 AM-10 PM) -------------------------


def build_morning_alert(now: Optional[datetime] = None) -> Optional[str]:
    """One combined message for today's special conditions, or None = silence.

    Parts are separated by blank lines so delivery sends them as chat bubbles.
    """
    now = now or datetime.now(ET)
    data = fetch_forecast()
    if data is None:
        return None
    today_rows = [
        r for r in _hourly_rows(data)
        if r["dt"].date() == now.date() and r["dt"].hour in MORNING_SCAN_HOURS
    ]
    if not today_rows:
        return None

    parts: list[str] = []

    rain_hours = [
        r for r in today_rows
        if (r.get("precipitation_probability") or 0) >= RAIN_PROB_THRESHOLD
    ]
    if len(rain_hours) == 1:
        r = rain_hours[0]
        parts.append(_pick(RAIN_SINGLE, t=_clock(r["dt"].hour),
                           p=round(r["precipitation_probability"])))
    elif rain_hours:
        peak = max(rain_hours, key=lambda r: r["precipitation_probability"])
        del peak  # range template carries no probability; peak kept for future use
        parts.append(_pick(RAIN_RANGE, a=_clock(rain_hours[0]["dt"].hour),
                           b=_clock(rain_hours[-1]["dt"].hour)))

    snow_hours = [r for r in today_rows if (r.get("snowfall") or 0) > 0]
    if snow_hours:
        parts.append(_pick(SNOW, part=_part_of_day(snow_hours[0]["dt"].hour)))

    apparent = [r for r in today_rows if r.get("apparent_temperature") is not None]
    if apparent:
        hottest = max(apparent, key=lambda r: r["apparent_temperature"])
        if hottest["apparent_temperature"] >= HEAT_F:
            parts.append(_pick(HEAT, t=round(hottest["apparent_temperature"]),
                               h=_clock(hottest["dt"].hour)))
        coldest = min(apparent, key=lambda r: r["apparent_temperature"])
        if coldest["apparent_temperature"] <= COLD_F:
            parts.append(_pick(COLD, t=round(coldest["apparent_temperature"])))

    return "\n\n".join(parts) if parts else None


# --- AQI monitor (every 2h, 8 AM-8 PM) -----------------------------------


def aqi_band(aqi: float) -> str:
    if aqi >= AQI_ALERT:
        return "bad"
    if aqi > AQI_SOFT:
        return "soft"
    return "good"


def fetch_current_aqi(now: Optional[datetime] = None) -> Optional[float]:
    now = now or datetime.now(ET)
    data = fetch_air_quality()
    if data is None:
        return None
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for row in _hourly_rows(data):
        if row["dt"] == current_hour and row.get("us_aqi") is not None:
            return float(row["us_aqi"])
    log.warning("No AQI value found for %s", current_hour)
    return None


def evaluate_aqi(aqi: float, state: dict, today: str) -> tuple[Optional[str], dict]:
    """Banded dedupe: (message-or-None, new state). Pure — no I/O.

    State: {"date", "alerted_band" (None|"soft"|"bad"), "recovery_sent"}.
    Re-alert only on crossing into a HIGHER band or on a new day. If a "bad"
    alert fired and AQI falls back to good, send one all-clear.
    """
    if state.get("date") != today:
        state = {"date": today, "alerted_band": None, "recovery_sent": False}
    else:
        state = dict(state)  # never mutate the caller's copy

    band = aqi_band(aqi)
    alerted = state.get("alerted_band")
    message = None

    if band == "bad" and alerted != "bad":
        message = _pick(AQI_BAD, aqi=round(aqi))
        state["alerted_band"] = "bad"
        state["recovery_sent"] = False
    elif band == "soft" and alerted is None:  # once per day max, and never after a "bad"
        message = _pick(AQI_SOFT_MSG, aqi=round(aqi))
        state["alerted_band"] = "soft"
    elif band == "good" and alerted == "bad" and not state.get("recovery_sent"):
        message = _pick(AQI_RECOVERY)
        state["recovery_sent"] = True

    return message, state


# --- Current conditions for the advisor ----------------------------------


def get_current_conditions(now: Optional[datetime] = None) -> Optional[dict]:
    """Snapshot for the advisor: is outside a good idea right now?

    Returns {aqi, aqi_band, rain_next_3h, snow_next_3h, feels_like_f} with
    None fields for whatever couldn't be fetched; returns None only if
    nothing at all was available.
    """
    now = now or datetime.now(ET)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    window_end = current_hour + timedelta(hours=3)

    conditions: dict = {
        "aqi": None, "aqi_band": None,
        "rain_next_3h": None, "snow_next_3h": None, "feels_like_f": None,
    }
    got_anything = False

    forecast = fetch_forecast()
    if forecast is not None:
        window = [
            r for r in _hourly_rows(forecast)
            if current_hour <= r["dt"] <= window_end
        ]
        if window:
            got_anything = True
            conditions["rain_next_3h"] = any(
                (r.get("precipitation_probability") or 0) >= RAIN_PROB_THRESHOLD
                for r in window
            )
            conditions["snow_next_3h"] = any(
                (r.get("snowfall") or 0) > 0 for r in window
            )
            if window[0].get("apparent_temperature") is not None:
                conditions["feels_like_f"] = round(window[0]["apparent_temperature"])

    aqi = fetch_current_aqi(now)
    if aqi is not None:
        got_anything = True
        conditions["aqi"] = round(aqi)
        conditions["aqi_band"] = aqi_band(aqi)

    return conditions if got_anything else None
