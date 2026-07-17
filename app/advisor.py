"""Generate the morning brief with the Claude API.

Given last night's SleepSummary, today's events, and the free slots we found,
ask Claude (claude-sonnet-4-6) for a short morning brief AND — when sleep was
poor — a structured "restore_block" proposal (a time + activity) we can write
back to the calendar.

The model is instructed to return ONLY a single JSON object:

    {"brief": "<text>", "restore_block": {"start": "<iso8601>", "activity": "<str>"} | null}

We parse it defensively (claude-sonnet-4-6 doesn't support the strict
`output_config.format` path, so we prompt for JSON and tolerate stray fences /
prose). The restore_block's `start` must be copied verbatim from a provided
free slot; main.py re-validates it server-side before ever writing an event.

Reads ANTHROPIC_API_KEY and CLAUDE_MODEL from the environment.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from anthropic import Anthropic
from pydantic import BaseModel

from app import persona
from app.calendar_client import Event, TimeSlot
from app.sleep import SleepSummary

log = logging.getLogger(__name__)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 500  # brief + small JSON envelope.

# Sleep tiers that warrant writing a recovery block back to the calendar.
RESTORE_FLAGS = ("short", "fragmented", "poor")


class RestoreBlock(BaseModel):
    start: datetime          # must equal one of the provided free slots' starts
    activity: str            # short recovery action, e.g. "walk outside"


class BriefResult(BaseModel):
    brief: str
    restore_block: Optional[RestoreBlock] = None


SYSTEM_PROMPT = """\
You are a calm, direct personal morning advisor (your name and vibe are set \
above). You are warm but you never pad your words: no fluff, no filler, no \
motivational-poster language.

You receive JSON with last night's sleep metrics, today's calendar events, and \
the free time slots available today (each free slot has a human `start`/`end` \
label and a machine `start_iso`).

Respond with ONLY a single JSON object — no prose, no markdown fences, nothing \
before or after it — of exactly this shape:

{"brief": "<the spoken morning brief>", "restore_block": {"start": "<iso8601>", "activity": "<2-4 word activity>"} | null}

Writing the "brief" field:
- Ground every claim in the data. Never invent events, metrics, or free slots.
- Frame everything as scheduling and energy management. You are NOT a doctor: \
never give medical, diagnostic, or treatment advice, and never name health \
conditions.
- No emoji. At most one exclamation mark.
- If quality_flag is "good": say so plainly, keep the brief to at most 2 \
sentences, and do not manufacture problems.
- Otherwise write at most 4 sentences, in order: (1) an honest energy \
assessment from the sleep numbers; (2) one concrete flag about a specific \
event today and why it may be demanding; (3) one concrete recovery action \
tied to a real free slot, naming its exact clock time; (4) one short line of \
encouragement.

Setting the "restore_block" field:
- If quality_flag is "good": set restore_block to null.
- If quality_flag is "short", "fragmented", or "poor": pick ONE of the \
provided free_slots for a short recovery activity. Set "start" to that slot's \
`start_iso`, copied EXACTLY, character for character. Set "activity" to a \
short 2-4 word recovery action (e.g. "walk outside", "nap", "quiet tea"). The \
time you name in the brief must be that same slot.
- Never invent a "start" — it must be a verbatim copy of a free slot's \
`start_iso`.

Return only the JSON object."""


CHAT_SYSTEM_PROMPT = """\
You are a warm, calm morning companion texting in a Telegram chat (your name and
vibe are set above). Same voice as the morning brief: gentle, direct, zero fluff,
chat-native (a lowercase, relaxed register is fine, an emoji or two is welcome).
You are NOT a notification and NOT a chatbot reading a script — you're the friend
who sent this morning's brief.

You're mid-conversation with the person you sent this morning's brief to. A JSON
"context" block gives you their sleep last night, this morning's brief, today's
remaining events, and the free slots still open today.

Rules:
- Keep replies SHORT: 1-4 sentences. Never lecture, never over-explain, never
  give a numbered list of tips.
- For timing questions (nap, break, moving something), anchor to a REAL free slot
  or event from the context, naming the exact clock time. Never invent a time.
- You are NOT a doctor: frame everything as scheduling and energy management,
  never medical/diagnostic advice, never name conditions. If they sound genuinely
  unwell, be kind, suggest rest, and gently point them to a real professional.
- If they ask something outside sleep/schedule, answer briefly and kindly in a
  sentence — don't refuse coldly, don't pretend expertise.
- Plain text only. No markdown, no headings."""


def generate_reply(user_text: str, *, context: dict, conversation: list,
                   temperature: Optional[float] = None) -> str:
    """Reply to a chat message in the companion voice, grounded in `context`.

    `conversation` is the rolling window of prior turns ([{role, text}, ...]);
    the morning brief is carried in `context`, not as a message turn.
    """
    client = Anthropic()
    system = (persona.prompt() + "\n\n" + CHAT_SYSTEM_PROMPT
              + "\n\nContext (JSON):\n" + json.dumps(context, indent=2))

    messages = [
        {"role": "user" if t["role"] == "user" else "assistant", "content": t["text"]}
        for t in conversation
    ]
    messages.append({"role": "user", "content": user_text})

    log.info("Calling Claude (%s) for chat reply (%d prior turn(s))", MODEL, len(conversation))
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        thinking={"type": "disabled"},
        system=system,
        messages=messages,
        **kwargs,
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    log.info("Chat reply generated (%d chars, request_id=%s)", len(text), response._request_id)
    return text


def _time_label(dt: datetime) -> str:
    """Human clock time like '3:30 PM' (drop the leading zero on the hour)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _build_payload(sleep: SleepSummary, events: list[Event], free_slots: list[TimeSlot]) -> dict:
    """Assemble the structured data we hand to the model."""
    return {
        "sleep": {
            "total_hours": sleep.total_hours,
            "deep_minutes": sleep.deep_minutes,
            "rem_minutes": sleep.rem_minutes,
            "awake_minutes": sleep.awake_minutes,
            "resting_hr": sleep.resting_hr,
            "hrv_ms": sleep.hrv_ms,
            "quality_flag": sleep.quality_flag,
        },
        "today_events": [
            {
                "title": e.title,
                "start": _time_label(e.start),
                "end": _time_label(e.end),
                "is_all_day": e.is_all_day,
            }
            for e in events
        ],
        "free_slots": [
            {
                "start": _time_label(s.start),
                "end": _time_label(s.end),
                "start_iso": s.start.isoformat(),  # what restore_block.start must copy
                "duration_minutes": s.duration_minutes,
            }
            for s in free_slots
        ],
    }


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model response, tolerating fences/prose."""
    t = text.strip()
    if t.startswith("```"):
        # drop a leading ```json / ``` fence and any trailing fence
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model response")
    return t[start : end + 1]


def _parse_brief(text: str) -> BriefResult:
    """Parse the model response into a BriefResult, degrading gracefully.

    If the JSON can't be parsed, we keep the raw text as the brief and drop the
    restore_block — better a plain brief than a crash.
    """
    try:
        raw = json.loads(_extract_json(text))
        return BriefResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — defensive parse; failure is logged
        log.warning("Could not parse structured brief JSON (%s); using raw text", exc)
        return BriefResult(brief=text.strip(), restore_block=None)


def generate_brief(
    sleep: SleepSummary,
    events: list[Event],
    free_slots: list[TimeSlot],
    *,
    temperature: Optional[float] = None,
) -> BriefResult:
    """Generate the morning brief (and optional restore-block proposal)."""
    client = Anthropic()  # reads ANTHROPIC_API_KEY (or an `ant` profile).
    payload = _build_payload(sleep, events, free_slots)

    log.info(
        "Calling Claude (%s) for morning brief: quality=%s, %d event(s), %d free slot(s)",
        MODEL, sleep.quality_flag, len(events), len(free_slots),
    )

    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},  # small structured output; no reasoning pass.
        system=persona.prompt() + "\n\n" + SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        **kwargs,
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    result = _parse_brief(text)
    log.info(
        "Claude brief parsed (%d chars, restore_block=%s, stop_reason=%s, request_id=%s)",
        len(result.brief), result.restore_block is not None,
        response.stop_reason, response._request_id,
    )
    return result
