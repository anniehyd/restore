"""Reset the demo to a known bad-night state so you can re-run it on stage.

Default (offline): writes a canned snapshot straight to the store from the
bad-night fixture — no Claude call, no calendar write, no phone push. The demo
page (GET /) shows a consistent "poor night" every time. Fast and reliable.

    python scripts/demo_reset.py

Live mode: actually POST the fixture through the running server's /wake so the
full pipeline fires (brief + push + optional calendar write). Uses WEBHOOK_SECRET
from the env if set.

    python scripts/demo_reset.py --live http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import demo_seed, store

ET = ZoneInfo("America/New_York")
BAD_NIGHT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sleep_bad_night.json"


def offline_reset() -> int:
    seed = demo_seed.seed(datetime.now(ET))
    print(f"Demo reset (offline). Snapshot written to {store._state_path()}.")
    print(f"Proposed (un-booked) Restore block at {seed['slot_label']} — tap 'book it' in "
          f"Telegram (or run --live) to book it.")
    print("Open GET / to see the bad-night demo. No push / no calendar write performed.")
    return 0


def live_reset(base_url: str) -> int:
    import httpx

    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("WEBHOOK_SECRET")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    resp = httpx.post(
        f"{base_url.rstrip('/')}/wake",
        content=BAD_NIGHT.read_bytes(),
        headers=headers,
        timeout=30.0,
    )
    print(f"POST {base_url}/wake -> {resp.status_code}")
    print(resp.text)
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="BASE_URL",
                    help="POST the fixture through a running server instead of resetting offline")
    args = ap.parse_args()
    return live_reset(args.live) if args.live else offline_reset()


if __name__ == "__main__":
    raise SystemExit(main())
