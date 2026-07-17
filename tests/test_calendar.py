"""Tests for calendar parsing and free-slot detection. No network calls."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import calendar_client as cal
from app.calendar_client import Event, find_free_slots, get_today_events

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


@pytest.fixture
def busy_day() -> list[Event]:
    """A realistic busy Thursday: two classes, a client meeting, a workout."""
    return [
        Event(title="Linear Algebra",  start=_et(2026, 7, 16, 9, 30),  end=_et(2026, 7, 16, 10, 45), is_all_day=False),
        Event(title="Algorithms",      start=_et(2026, 7, 16, 11, 0),  end=_et(2026, 7, 16, 12, 15), is_all_day=False),
        Event(title="Client meeting",  start=_et(2026, 7, 16, 14, 0),  end=_et(2026, 7, 16, 15, 0),  is_all_day=False),
        Event(title="Workout",         start=_et(2026, 7, 16, 18, 0),  end=_et(2026, 7, 16, 19, 0),  is_all_day=False),
    ]


# --- find_free_slots --------------------------------------------------------


def test_free_slots_on_busy_day(busy_day):
    slots = find_free_slots(busy_day, min_minutes=20)

    # With a 30-min buffer, the two morning classes merge into one busy block
    # (9:00–12:45). Expected gaps within 9am–9pm:
    #   12:45–13:30 (45m), 15:30–17:30 (120m), 19:30–21:00 (90m)
    assert [s.duration_minutes for s in slots] == [45, 120, 90]

    first = slots[0]
    assert first.start == _et(2026, 7, 16, 12, 45)
    assert first.end == _et(2026, 7, 16, 13, 30)

    last = slots[-1]
    assert last.start == _et(2026, 7, 16, 19, 30)
    assert last.end == _et(2026, 7, 16, 21, 0)


def test_min_minutes_filters_short_gaps(busy_day):
    # Raising the threshold to 60 min drops the 45-min lunch gap.
    slots = find_free_slots(busy_day, min_minutes=60)
    assert [s.duration_minutes for s in slots] == [120, 90]


def test_all_day_event_blocks_everything():
    events = [Event(title="Conference (all day)", start=_et(2026, 7, 16, 0, 0),
                    end=_et(2026, 7, 17, 0, 0), is_all_day=True)]
    assert find_free_slots(events) == []


def test_empty_day_is_one_big_slot():
    # No events -> the whole 9am–9pm waking window is free (12h = 720 min).
    slots = find_free_slots([])
    assert len(slots) == 1
    assert slots[0].duration_minutes == 720


# --- get_today_events (API layer mocked) ------------------------------------


def test_get_today_events_parses_timed_and_all_day(monkeypatch):
    raw = [
        {
            "summary": "Algorithms",
            "start": {"dateTime": "2026-07-16T11:00:00-04:00"},
            "end": {"dateTime": "2026-07-16T12:15:00-04:00"},
        },
        {
            "summary": "Move-out day",
            "start": {"date": "2026-07-16"},
            "end": {"date": "2026-07-17"},
        },
        {
            # no summary -> falls back to a placeholder title
            "start": {"dateTime": "2026-07-16T14:00:00-04:00"},
            "end": {"dateTime": "2026-07-16T15:00:00-04:00"},
        },
    ]

    captured = {}

    def fake_fetch(calendar_id, time_min, time_max):
        captured["calendar_id"] = calendar_id
        captured["time_min"] = time_min
        captured["time_max"] = time_max
        return raw

    monkeypatch.setattr(cal, "_fetch_raw_events", fake_fetch)

    now = _et(2026, 7, 16, 8, 0)
    events = get_today_events(calendar_id="primary", now=now)

    assert [e.title for e in events] == ["Algorithms", "Move-out day", "(no title)"]
    assert events[0].is_all_day is False
    assert events[1].is_all_day is True
    assert events[0].start == _et(2026, 7, 16, 11, 0)  # normalized to ET

    # window: from `now` to 11pm the same day
    assert captured["calendar_id"] == "primary"
    assert captured["time_min"] == now
    assert captured["time_max"] == _et(2026, 7, 16, 23, 0)


def test_get_today_events_after_cutoff_returns_empty(monkeypatch):
    def boom(*a, **k):  # must not be called
        raise AssertionError("should not fetch after cutoff")

    monkeypatch.setattr(cal, "_fetch_raw_events", boom)
    late = _et(2026, 7, 16, 23, 30)
    assert get_today_events(now=late) == []
