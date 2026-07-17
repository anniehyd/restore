"""Dry-run the full brief pipeline against a fixture, without the watch.

Parses the bad-night sleep fixture, builds a fake busy calendar, finds free
slots, and prints N generated briefs so you can iterate on the prompt in
app/advisor.py. Hits the real Claude API — needs ANTHROPIC_API_KEY.

Usage:
    python scripts/dry_run.py            # 3 samples from the bad-night fixture
    python scripts/dry_run.py --runs 5
    python scripts/dry_run.py --fixture good   # use the good-night fixture
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.advisor import generate_brief
from app.calendar_client import Event, find_free_slots
from app.sleep import parse_sleep

ET = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _et(hh: int, mm: int) -> datetime:
    # A fixed sample day; the advisor only cares about clock times.
    return datetime(2026, 7, 16, hh, mm, tzinfo=ET)


def fake_busy_day() -> list[Event]:
    """A realistic packed day: two classes, a client meeting, a workout."""
    return [
        Event(title="Linear Algebra", start=_et(9, 30), end=_et(10, 45), is_all_day=False),
        Event(title="Algorithms",     start=_et(11, 0), end=_et(12, 15), is_all_day=False),
        Event(title="Client meeting",  start=_et(14, 0), end=_et(15, 0),  is_all_day=False),
        Event(title="Workout",         start=_et(18, 0), end=_et(19, 0),  is_all_day=False),
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="number of sample briefs to print")
    ap.add_argument("--fixture", choices=["bad", "good"], default="bad")
    ap.add_argument("--temperature", type=float, default=1.0,
                    help="sampling temperature; higher = more varied samples")
    args = ap.parse_args()

    payload = json.loads((FIXTURES / f"sleep_{args.fixture}_night.json").read_text())
    sleep = parse_sleep(payload)
    events = fake_busy_day()
    free_slots = find_free_slots(events)

    print(f"\n=== Sleep summary ({args.fixture} night) ===")
    print(f"  {sleep.total_hours}h total | deep {sleep.deep_minutes}m | rem {sleep.rem_minutes}m "
          f"| awake {sleep.awake_minutes}m | RHR {sleep.resting_hr} | HRV {sleep.hrv_ms} "
          f"| flag={sleep.quality_flag}")
    print(f"  free slots: " + ", ".join(
        f"{s.start.strftime('%I:%M %p').lstrip('0')}-{s.end.strftime('%I:%M %p').lstrip('0')} "
        f"({s.duration_minutes}m)" for s in free_slots))

    slot_starts = {s.start for s in free_slots}

    for i in range(1, args.runs + 1):
        result = generate_brief(sleep, events, free_slots, temperature=args.temperature)
        print(f"\n--- Brief {i}/{args.runs} ---")
        print(result.brief)
        rb = result.restore_block
        if rb is None:
            print("[restore_block: none]")
        elif rb.start in slot_starts:
            when = rb.start.strftime("%I:%M %p").lstrip("0")
            # Dry run NEVER writes, regardless of CALENDAR_WRITE_ENABLED.
            print(f"[WOULD CREATE] 🌿 Restore: {rb.activity} at {when} (20 min) — ✓ matches a free slot")
        else:
            print(f"[restore_block REJECTED] start {rb.start.isoformat()} is not in the free-slot list")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
