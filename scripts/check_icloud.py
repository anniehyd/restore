"""Verify iCloud CalDAV credentials in one command.

Connects with ICLOUD_USERNAME / ICLOUD_APP_PASSWORD, lists your calendars,
shows which one the app will use, prints today's remaining events, and (with
--write) creates + immediately deletes a throwaway event to confirm write access.

    ICLOUD_USERNAME=you@icloud.com ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop \
        python scripts/check_icloud.py            # read-only check
    ... python scripts/check_icloud.py --write     # also test writing
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

from app import calendar_client as cal


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="also create + delete a test event")
    args = ap.parse_args()

    if not os.environ.get("ICLOUD_USERNAME") or not os.environ.get("ICLOUD_APP_PASSWORD"):
        print("ERROR: set ICLOUD_USERNAME and ICLOUD_APP_PASSWORD (app-specific password).",
              file=sys.stderr)
        return 1

    try:
        calendar = cal._open_calendar()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to connect: {exc}", file=sys.stderr)
        print("Tip: use an APP-SPECIFIC password (appleid.apple.com), not your Apple ID password.",
              file=sys.stderr)
        return 1

    print(f"Connected as {os.environ['ICLOUD_USERNAME']}")

    import caldav  # list all calendars for context
    client = caldav.DAVClient(url=cal.CALDAV_URL,
                              username=os.environ["ICLOUD_USERNAME"],
                              password=os.environ["ICLOUD_APP_PASSWORD"])
    names = [c.name for c in client.principal().calendars()]
    print(f"Calendars found: {names}")
    print(f"Using: {calendar.name!r}\n")

    print("Today's events (now..11pm):")
    events = cal.get_today_events()
    if not events:
        print("  (none)")
    for e in events:
        when = "all-day" if e.is_all_day else e.start.strftime("%I:%M %p").lstrip("0")
        print(f"  • {when:>8}  {e.title}")

    slots = cal.find_free_slots(events)
    print("\nFree slots:")
    for s in slots:
        a, b = (t.strftime("%I:%M %p").lstrip("0") for t in (s.start, s.end))
        print(f"  • {a}–{b}  ({s.duration_minutes}m)")

    if args.write:
        from app.calendar_client import TimeSlot, RESTORE_BLOCK_MINUTES
        start = datetime.now(cal.LOCAL_TZ) + timedelta(minutes=5)
        slot = TimeSlot(start=start, end=start + timedelta(minutes=RESTORE_BLOCK_MINUTES),
                        duration_minutes=RESTORE_BLOCK_MINUTES)
        print("\nWriting a throwaway test event...")
        ical = cal._restore_event_ical(slot, "connectivity test")
        obj = calendar.save_event(ical)
        print(f"  created: {getattr(obj, 'url', '?')}")
        obj.delete()
        print("  deleted. Write access confirmed ✓")

    print("\nAll good ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
