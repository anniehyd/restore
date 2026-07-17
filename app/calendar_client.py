"""Google Calendar read/write via the Calendar API v3 (installed-app OAuth).

This is a personal, single-user project, so we use the installed-app OAuth
flow: a one-time local authorization (see scripts/authorize.py) writes a
refresh token to token.json, and this module loads/refreshes it on demand.

Public API:
  - get_today_events(calendar_id="primary") -> list[Event]
  - find_free_slots(events, min_minutes=20) -> list[TimeSlot]

Times are handled in the local timezone (America/New_York).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel

log = logging.getLogger(__name__)

# Read + write: write is needed for the stretch "Restore" recovery-block feature.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

LOCAL_TZ = ZoneInfo("America/New_York")

# Where the OAuth client-secrets JSON (downloaded from Google Cloud Console)
# and the resulting refresh token live. Both are gitignored.
CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")

# Waking hours we're willing to schedule recovery in, and the dead-air buffer
# we keep around each event so a "free" slot isn't wedged against a meeting.
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


# --- OAuth / service --------------------------------------------------------


GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _credentials_from_env() -> Optional[Credentials]:
    """Build credentials from GOOGLE_REFRESH_TOKEN + client id/secret, if set.

    This is the deployment path: no token.json on disk, nothing sensitive in the
    repo — just three secrets injected as env vars. Returns None if
    GOOGLE_REFRESH_TOKEN isn't set (fall back to the local token.json flow).
    """
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not refresh_token:
        return None

    try:
        client_id = os.environ["GOOGLE_CLIENT_ID"]
        client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    except KeyError as exc:
        raise RuntimeError(f"GOOGLE_REFRESH_TOKEN is set but {exc} is missing") from exc

    creds = Credentials(
        token=None,  # no access token yet; refresh() mints one
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=SCOPES,
    )
    log.info("Loading Google credentials from environment (refresh token)")
    creds.refresh(Request())
    return creds


def _load_credentials() -> Credentials:
    """Credentials from env (deployment) or token.json (local). Fails loudly."""
    env_creds = _credentials_from_env()
    if env_creds is not None:
        return env_creds

    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"No credentials found. Either set GOOGLE_REFRESH_TOKEN / "
            f"GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (deployment), or run "
            f"`python scripts/authorize.py` to create {TOKEN_PATH} (local)."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        log.info("Refreshing expired Google Calendar token")
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())
        return creds

    raise RuntimeError(
        f"{TOKEN_PATH} is present but invalid and cannot be refreshed. "
        f"Re-run `python scripts/authorize.py`."
    )


def _calendar_service():
    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)


def _fetch_raw_events(calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Call the Calendar API and return the raw event items. Mocked in tests."""
    log.info(
        "Calendar API events.list calendar=%s window=%s..%s",
        calendar_id, time_min.isoformat(), time_max.isoformat(),
    )
    service = _calendar_service()
    resp = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    items = resp.get("items", [])
    log.info("Calendar API returned %d event(s)", len(items))
    return items


# --- Parsing ----------------------------------------------------------------


def _parse_event(item: dict) -> Event:
    """Convert one raw Calendar API event into our Event model (local tz)."""
    start_raw = item["start"]
    end_raw = item["end"]

    if "date" in start_raw:  # all-day event: {"date": "2026-07-16"}
        start = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=LOCAL_TZ)
        end = datetime.fromisoformat(end_raw["date"]).replace(tzinfo=LOCAL_TZ)
        is_all_day = True
    else:  # timed event: {"dateTime": "2026-07-16T09:30:00-04:00"}
        start = datetime.fromisoformat(start_raw["dateTime"]).astimezone(LOCAL_TZ)
        end = datetime.fromisoformat(end_raw["dateTime"]).astimezone(LOCAL_TZ)
        is_all_day = False

    return Event(
        title=item.get("summary", "(no title)"),
        start=start,
        end=end,
        is_all_day=is_all_day,
    )


# --- Public API -------------------------------------------------------------


def get_today_events(calendar_id: str = "primary", *, now: Optional[datetime] = None) -> list[Event]:
    """Return today's events from `now` until 11pm local time.

    `now` is injectable for testing; in production it defaults to the current
    local time. Returns [] if it's already past the 11pm cutoff.
    """
    now = now or datetime.now(LOCAL_TZ)
    cutoff = now.replace(hour=DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        log.info("get_today_events called after %02d:00 cutoff; nothing to fetch", DAY_CUTOFF_HOUR)
        return []

    raw = _fetch_raw_events(calendar_id, now, cutoff)
    return [_parse_event(item) for item in raw]


def _waking_window(events: list[Event]) -> tuple[datetime, datetime]:
    """The 9am–9pm waking window for the relevant day.

    The day is taken from the earliest event, or today if there are no events.
    """
    if events:
        day: date = min(e.start for e in events).astimezone(LOCAL_TZ).date()
    else:
        day = datetime.now(LOCAL_TZ).date()
    start = datetime.combine(day, time(WAKING_START_HOUR, 0), tzinfo=LOCAL_TZ)
    end = datetime.combine(day, time(WAKING_END_HOUR, 0), tzinfo=LOCAL_TZ)
    return start, end


def find_free_slots(events: list[Event], min_minutes: int = 20) -> list[TimeSlot]:
    """Find free gaps of at least `min_minutes` during waking hours (9am–9pm).

    Each event is padded by a 30-minute buffer on both sides, all-day events
    block the whole waking window, and overlapping busy blocks are merged before
    the remaining gaps are returned.
    """
    waking_start, waking_end = _waking_window(events)
    buffer = timedelta(minutes=EVENT_BUFFER_MINUTES)

    # Build busy intervals, padded and clipped to the waking window.
    busy: list[tuple[datetime, datetime]] = []
    for e in events:
        if e.is_all_day:
            busy.append((waking_start, waking_end))
        else:
            busy.append((e.start - buffer, e.end + buffer))

    clipped: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        s = max(s, waking_start)
        e = min(e, waking_end)
        if s < e:
            clipped.append((s, e))

    # Merge overlapping/adjacent busy intervals.
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(clipped):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # Walk the gaps between busy blocks.
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


def _restore_event_body(slot: TimeSlot, suggestion: str) -> dict:
    """The Google Calendar event body for a 20-minute recovery block.

    Factored out so callers (and the dry-run) can inspect exactly what would be
    inserted without hitting the API.
    """
    start = slot.start
    end = start + timedelta(minutes=RESTORE_BLOCK_MINUTES)
    return {
        "summary": f"🌿 Restore: {suggestion}",
        "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/New_York"},
        "description": "Auto-created by Restore, your sleep-aware morning advisor.",
    }


def create_restore_block(slot: TimeSlot, suggestion: str) -> dict:
    """Insert a 20-minute "🌿 Restore: <suggestion>" event and return it.

    Writes into GOOGLE_CALENDAR_ID (default "primary"). Requires calendar write
    scope (already in SCOPES) and a valid token. Raises on any API failure.
    """
    body = _restore_event_body(slot, suggestion)
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    log.info(
        "Calendar API events.insert calendar=%s summary=%r start=%s",
        calendar_id, body["summary"], body["start"]["dateTime"],
    )
    service = _calendar_service()
    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    log.info("Created event id=%s link=%s", event.get("id"), event.get("htmlLink"))
    return event
