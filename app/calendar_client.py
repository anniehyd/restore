"""Apple / iCloud Calendar read + write via CalDAV.

Apple has no Google-style REST API, so we talk to iCloud over CalDAV using an
app-specific password (generate one at appleid.apple.com → Sign-In & Security →
App-Specific Passwords). No OAuth dance, no token file — just two env vars.

Public API (unchanged from before):
  - get_today_events() -> list[Event]
  - find_free_slots(events, min_minutes=20) -> list[TimeSlot]
  - create_restore_block(slot, suggestion) -> dict

Env:
  ICLOUD_USERNAME       your Apple ID email
  ICLOUD_APP_PASSWORD   an app-specific password (NOT your Apple ID password)
  ICLOUD_CALENDAR_NAME  optional; which calendar to use (default: the first one)
  CALDAV_URL            optional; default https://caldav.icloud.com

Times are handled in the local timezone (America/New_York).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import caldav
import icalendar
from pydantic import BaseModel

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/New_York")

CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com")

# Waking hours we schedule recovery in, and the dead-air buffer around events.
WAKING_START_HOUR = 9   # 9am
WAKING_END_HOUR = 21    # 9pm
EVENT_BUFFER_MINUTES = 30
DAY_CUTOFF_HOUR = 23    # only look at events from now until 11pm today
RESTORE_BLOCK_MINUTES = 20  # length of the recovery block we write back


# --- Models -----------------------------------------------------------------


class Event(BaseModel):
    title: str
    start: datetime
    end: datetime
    is_all_day: bool


class TimeSlot(BaseModel):
    start: datetime
    end: datetime
    duration_minutes: int


# --- CalDAV connection ------------------------------------------------------


def _open_calendar() -> "caldav.Calendar":
    """Connect to iCloud and return the target calendar. Fails loudly."""
    username = os.environ.get("ICLOUD_USERNAME")
    password = os.environ.get("ICLOUD_APP_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set. Generate an "
            "app-specific password at appleid.apple.com and set both."
        )

    client = caldav.DAVClient(url=CALDAV_URL, username=username, password=password)
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise RuntimeError("No iCloud calendars found for this Apple ID")

    wanted = os.environ.get("ICLOUD_CALENDAR_NAME")
    if wanted:
        for cal in calendars:
            if cal.name == wanted:
                return cal
        raise RuntimeError(
            f"Calendar {wanted!r} not found; available: {[c.name for c in calendars]}"
        )
    return calendars[0]


def _search_events(time_min: datetime, time_max: datetime) -> list:
    """Return icalendar VEVENT components in the window. Mocked in tests."""
    cal = _open_calendar()
    log.info("CalDAV search window=%s..%s", time_min.isoformat(), time_max.isoformat())
    results = cal.search(start=time_min, end=time_max, event=True, expand=True)
    comps = [r.icalendar_component for r in results]
    log.info("CalDAV returned %d event(s)", len(comps))
    return comps


# --- Parsing ----------------------------------------------------------------


def _to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=LOCAL_TZ) if dt.tzinfo is None else dt.astimezone(LOCAL_TZ)


def _parse_component(comp) -> Event:
    """Convert one icalendar VEVENT into our Event model (local tz)."""
    title = str(comp.get("summary", "(no title)"))
    dtstart = comp.get("dtstart").dt
    dtend_prop = comp.get("dtend")

    if isinstance(dtstart, datetime):  # timed event
        start = _to_local(dtstart)
        end = _to_local(dtend_prop.dt) if dtend_prop is not None else start + timedelta(hours=1)
        is_all_day = False
    else:  # all-day event: dtstart is a date
        start = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=LOCAL_TZ)
        if dtend_prop is not None:
            d = dtend_prop.dt
            end = datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ)
        else:
            end = start + timedelta(days=1)
        is_all_day = True

    return Event(title=title, start=start, end=end, is_all_day=is_all_day)


# --- Public API -------------------------------------------------------------


def get_today_events(*, now: Optional[datetime] = None) -> list[Event]:
    """Return today's events from `now` until 11pm local time.

    `now` is injectable for testing; defaults to the current local time.
    Returns [] if it's already past the 11pm cutoff.
    """
    now = now or datetime.now(LOCAL_TZ)
    cutoff = now.replace(hour=DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        log.info("get_today_events called after %02d:00 cutoff; nothing to fetch", DAY_CUTOFF_HOUR)
        return []
    return [_parse_component(c) for c in _search_events(now, cutoff)]


def _waking_window(events: list[Event]) -> tuple[datetime, datetime]:
    """The 9am–9pm waking window for the relevant day (from the earliest event)."""
    if events:
        day: date = min(e.start for e in events).astimezone(LOCAL_TZ).date()
    else:
        day = datetime.now(LOCAL_TZ).date()
    start = datetime.combine(day, time(WAKING_START_HOUR, 0), tzinfo=LOCAL_TZ)
    end = datetime.combine(day, time(WAKING_END_HOUR, 0), tzinfo=LOCAL_TZ)
    return start, end


def find_free_slots(events: list[Event], min_minutes: int = 20) -> list[TimeSlot]:
    """Find free gaps of at least `min_minutes` during waking hours (9am–9pm).

    Each event is padded by a 30-minute buffer, all-day events block the whole
    window, and overlapping busy blocks are merged before the gaps are returned.
    """
    waking_start, waking_end = _waking_window(events)
    buffer = timedelta(minutes=EVENT_BUFFER_MINUTES)

    busy: list[tuple[datetime, datetime]] = []
    for e in events:
        if e.is_all_day:
            continue  # birthdays, trips, all-day reminders don't block recovery time
        busy.append((e.start - buffer, e.end + buffer))

    clipped: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        s = max(s, waking_start)
        e = min(e, waking_end)
        if s < e:
            clipped.append((s, e))

    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(clipped):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    min_gap = timedelta(minutes=min_minutes)
    slots: list[TimeSlot] = []
    cursor = waking_start
    for s, e in merged:
        if s - cursor >= min_gap:
            slots.append(_slot(cursor, s))
        cursor = max(cursor, e)
    if waking_end - cursor >= min_gap:
        slots.append(_slot(cursor, waking_end))

    log.info("find_free_slots: %d slot(s) of >= %d min", len(slots), min_minutes)
    return slots


def _slot(start: datetime, end: datetime) -> TimeSlot:
    minutes = int((end - start).total_seconds() // 60)
    return TimeSlot(start=start, end=end, duration_minutes=minutes)


def _restore_event_ical(slot: TimeSlot, suggestion: str) -> str:
    """The iCalendar text for a 20-minute recovery block. Pure — no network."""
    start = slot.start
    end = start + timedelta(minutes=RESTORE_BLOCK_MINUTES)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Restore//sleep-aware morning advisor//EN")
    cal.add("version", "2.0")
    ev = icalendar.Event()
    ev.add("summary", f"🌿 Restore: {suggestion}")
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime.now(LOCAL_TZ))
    ev.add("uid", f"restore-{start.isoformat()}@restore.app")
    ev.add("description", "Auto-created by Restore, your sleep-aware morning advisor.")
    cal.add_component(ev)
    return cal.to_ical().decode()


def create_restore_block(slot: TimeSlot, suggestion: str) -> dict:
    """Write a 20-minute "🌿 Restore: <suggestion>" event to iCloud and return it.

    Requires ICLOUD_USERNAME / ICLOUD_APP_PASSWORD. Raises on any failure.
    """
    ical = _restore_event_ical(slot, suggestion)
    log.info("CalDAV save_event: 🌿 Restore: %s at %s", suggestion, slot.start.isoformat())
    cal = _open_calendar()
    obj = cal.save_event(ical)
    url = getattr(obj, "url", None)
    log.info("Created iCloud event %s", url)
    return {"summary": f"🌿 Restore: {suggestion}", "start": slot.start.isoformat(),
            "url": str(url) if url else None}
