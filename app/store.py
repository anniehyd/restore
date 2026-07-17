"""Dead-simple state store for the demo — one JSON file, no database.

Holds three things under one file (RESTORE_STATE_PATH, default ./state.json):
  - snapshot: the last morning brief (for /latest and the demo page)
  - conversation: a rolling window of the last ~10 chat exchanges
  - seen_update_ids: recent Telegram update_ids, for webhook dedupe

Access is serialized with a lock so the webhook's background tasks and /wake
don't clobber each other's read-modify-write.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

RESTORE_BLOCK_MINUTES = 20
MAX_MESSAGES = 20   # ~10 user/assistant exchanges
MAX_SEEN = 100      # recent update_ids kept for dedupe

_LOCK = threading.Lock()


def _state_path() -> Path:
    return Path(os.environ.get("RESTORE_STATE_PATH", "state.json"))


def _empty_state() -> dict:
    return {"snapshot": None, "conversation": [], "seen_update_ids": []}


def _read_state() -> dict:
    path = _state_path()
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read state from %s: %s", path, exc)
        return _empty_state()
    # Back-compat: an old file that is a bare snapshot (has "brief" at top level).
    if isinstance(data, dict) and "snapshot" not in data and "brief" in data:
        return {"snapshot": data, "conversation": [], "seen_update_ids": []}
    data.setdefault("snapshot", None)
    data.setdefault("conversation", [])
    data.setdefault("seen_update_ids", [])
    return data


def _write_state(state: dict) -> None:
    path = _state_path()
    try:
        path.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        log.error("Failed to write state to %s: %s", path, exc)


def _label(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def build_snapshot(
    *,
    sleep,
    events,
    brief: str,
    brief_source: str,
    restore: Optional[dict],
    generated_at: datetime,
) -> dict:
    """Assemble the JSON the demo page renders (see main._deliver for callers)."""
    core = max(round(sleep.total_hours * 60 - sleep.deep_minutes - sleep.rem_minutes, 1), 0.0)
    snapshot = {
        "generated_at": generated_at.isoformat(),
        "brief": brief,
        "brief_source": brief_source,
        "sleep": {
            "total_hours": sleep.total_hours,
            "deep_minutes": sleep.deep_minutes,
            "rem_minutes": sleep.rem_minutes,
            "core_minutes": core,
            "awake_minutes": sleep.awake_minutes,
            "resting_hr": sleep.resting_hr,
            "hrv_ms": sleep.hrv_ms,
            "quality_flag": sleep.quality_flag,
        },
        "events": [
            {
                "title": e.title,
                "start_iso": e.start.isoformat(),
                "end_iso": e.end.isoformat(),
                "start_label": _label(e.start),
                "end_label": _label(e.end),
                "is_all_day": e.is_all_day,
            }
            for e in events
        ],
        "restore": None,
    }
    if restore is not None:
        start = restore["start"]
        end = start + timedelta(minutes=RESTORE_BLOCK_MINUTES)
        snapshot["restore"] = {
            "start_iso": start.isoformat(),
            "end_iso": end.isoformat(),
            "start_label": _label(start),
            "activity": restore["activity"],
            "created": restore["created"],
        }
    return snapshot


def save_snapshot(snapshot: dict) -> None:
    """Store the morning snapshot and START A FRESH conversation for the day.

    Resetting the window here is what makes each morning a clean chat, seeded by
    that day's brief (which the chat always has in context via the snapshot).
    """
    with _LOCK:
        state = _read_state()
        state["snapshot"] = snapshot
        state["conversation"] = []
        _write_state(state)
    log.info("Saved snapshot and reset conversation window")


def load_snapshot() -> Optional[dict]:
    """Return the last morning snapshot, or None."""
    return _read_state().get("snapshot")


def mark_update_seen(update_id: int) -> bool:
    """Record a Telegram update_id. Returns True if new, False if already seen."""
    with _LOCK:
        state = _read_state()
        seen = state["seen_update_ids"]
        if update_id in seen:
            return False
        seen.append(update_id)
        del seen[:-MAX_SEEN]
        _write_state(state)
        return True


def append_exchange(user_text: str, assistant_text: str) -> None:
    """Append one user->assistant exchange, trimming to the rolling window."""
    with _LOCK:
        state = _read_state()
        conv = state["conversation"]
        conv.append({"role": "user", "text": user_text})
        conv.append({"role": "assistant", "text": assistant_text})
        del conv[:-MAX_MESSAGES]
        _write_state(state)


def get_conversation() -> list:
    """The current rolling window of chat turns."""
    return _read_state().get("conversation", [])


def mark_restore_booked() -> None:
    """Flip the snapshot's restore block to created=True (booked via the button)."""
    with _LOCK:
        state = _read_state()
        snap = state.get("snapshot")
        if snap and snap.get("restore"):
            snap["restore"]["created"] = True
            _write_state(state)
