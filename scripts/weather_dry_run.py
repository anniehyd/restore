"""Dry-run the weather/AQI alert pipeline: print what WOULD be sent, send nothing.

Usage:
    python scripts/weather_dry_run.py

Hits the real Open-Meteo APIs (no key needed) but never touches Telegram and
never writes alert state — safe to run any time.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store, weather  # noqa: E402


def main() -> int:
    now = datetime.now(weather.ET)
    print(f"=== Weather dry run — {now:%a %b %d, %I:%M %p} ET "
          f"({weather.WEATHER_LAT}, {weather.WEATHER_LON}) ===\n")

    print("--- Morning check (as if it were 7 AM today) ---")
    morning = weather.build_morning_alert(now)
    if morning:
        print(f"WOULD SEND:\n{morning}")
    else:
        print("(silence — no special conditions today, or fetch failed)")

    print("\n--- AQI check (right now) ---")
    aqi = weather.fetch_current_aqi(now)
    if aqi is None:
        print("(no AQI data available)")
    else:
        saved = store.get_weather_state().get("aqi", {})
        message, new_state = weather.evaluate_aqi(aqi, dict(saved), now.date().isoformat())
        print(f"current AQI: {aqi:.0f} (band: {weather.aqi_band(aqi)}, "
              f"saved state: {saved or 'none'})")
        if message:
            print(f"WOULD SEND: {message}")
        else:
            print("(silence — below thresholds or already alerted this band today)")
        print(f"state would become: {new_state}")

    print("\n--- Advisor conditions (get_current_conditions) ---")
    print(weather.get_current_conditions(now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
