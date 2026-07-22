"""Diagnostic: list today's + tomorrow's events on EVERY iCloud calendar."""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import caldav

ET = ZoneInfo("America/New_York")

client = caldav.DAVClient(
    url=os.environ.get("CALDAV_URL", "https://caldav.icloud.com"),
    username=os.environ["ICLOUD_USERNAME"],
    password=os.environ["ICLOUD_APP_PASSWORD"],
)

now = datetime.now(ET)
start = now.replace(hour=0, minute=0, second=0, microsecond=0)
end = start + timedelta(days=14)

print(f"Scanning {start:%a %b %d} 00:00 → {end:%a %b %d} 00:00 (ET)\n")
for cal in client.principal().calendars():
    name = cal.get_display_name()
    try:
        events = cal.search(start=start, end=end, event=True, expand=True)
    except Exception as exc:  # noqa: BLE001 — diagnostic; keep scanning
        print(f"[{name}] ERROR: {exc}")
        continue
    print(f"[{name}] {len(events)} event(s)")
    for ev in events:
        comp = ev.icalendar_component
        summary = str(comp.get("summary", "(untitled)"))
        dtstart = comp.get("dtstart")
        label = dtstart.dt.strftime("%a %I:%M %p") if hasattr(dtstart.dt, "hour") else f"{dtstart.dt} (all-day)"
        print(f"    • {label}  {summary}")
print("\nAnAn currently reads:", os.environ.get("ICLOUD_CALENDAR_NAME") or "(first calendar = Schedule)")
