"""Canned bad-night demo state, shared by scripts/demo_reset.py and /reset.

Builds a deterministic "poor night" snapshot (from the bad-night fixture + a fake
busy day) with a *proposed, not-yet-booked* Restore block, saves it (which resets
the conversation), and returns the pieces needed to send the morning message.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import store
from app.calendar_client import Event, find_free_slots
from app.sleep import parse_sleep

ET = ZoneInfo("America/New_York")
BAD_NIGHT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sleep_bad_night.json"

CANNED_BRIEF = (
    "morning 🌙 five hours and only 36 min of deep sleep, resting heart rate up "
    "at 66 — you're running on a deficit today. your 2:00 PM client meeting will "
    "hit hardest, right in the afternoon dip. i've teed up a walk at 3:30 PM to "
    "reset. you've got this 💛"
)
CANNED_ACTIVITY = "walk outside"


def _busy_day(now: datetime) -> list[Event]:
    d = now.date()

    def at(h, m):
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)

    return [
        Event(title="Linear Algebra", start=at(9, 30), end=at(10, 45), is_all_day=False),
        Event(title="Algorithms",     start=at(11, 0), end=at(12, 15), is_all_day=False),
        Event(title="Client meeting", start=at(14, 0), end=at(15, 0),  is_all_day=False),
        Event(title="Workout",        start=at(18, 0), end=at(19, 0),  is_all_day=False),
    ]


def seed(now: datetime) -> dict:
    """Write the canned snapshot; return {brief, slot_start_iso, slot_label, activity}."""
    sleep = parse_sleep(json.loads(BAD_NIGHT.read_text()))
    events = _busy_day(now)
    slot = find_free_slots(events)[1]  # the 3:30 PM slot
    snapshot = store.build_snapshot(
        sleep=sleep,
        events=events,
        brief=CANNED_BRIEF,
        brief_source="claude",
        restore={"start": slot.start, "activity": CANNED_ACTIVITY, "created": False},  # proposed, not booked
        generated_at=now,
    )
    store.save_snapshot(snapshot)
    return {
        "brief": CANNED_BRIEF,
        "slot_start_iso": slot.start.isoformat(),
        "slot_label": slot.start.strftime("%I:%M %p").lstrip("0"),
        "activity": CANNED_ACTIVITY,
    }
