"""AnAn — FastAPI app and webhook endpoint.

  - GET  /health  liveness check
  - POST /wake    the full morning flow:
        parse sleep -> fetch calendar -> generate brief -> push to phone
        (stretch) -> write a "🌿 Restore" recovery block into the calendar

/wake is resilient: a calendar failure degrades to a sleep-only brief, and a
Claude failure degrades to a plain fallback push built from the raw sleep
numbers. Every failure is logged; the brief is echoed in the HTTP response so
you can debug without your phone.

Calendar writes are gated behind CALENDAR_WRITE_ENABLED (default false) so the
read-only demo path is safe, and the model's proposed time is re-validated
against the real free-slot list before anything is written.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import ValidationError

from app import demo_seed, persona, store, weather
from app.advisor import RESTORE_FLAGS, generate_brief, generate_reply
from app.calendar_client import (
    Event,
    TimeSlot,
    create_restore_block,
    find_free_slots,
    get_today_events,
)
from app.demo_page import DEMO_HTML
from app.notify import send_push
from app.sleep import SleepSummary, parse_sleep
from app.store import RESTORE_BLOCK_MINUTES
from app.telegram_client import answer_callback, edit_message_text
from app.telegram_client import send_message as send_telegram
from app.whatsapp_client import send_message as send_whatsapp

LOCAL_TZ = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("restore")

app = FastAPI(title="AnAn", description="Sleep-aware morning companion")


@app.get("/health")
def health() -> dict:
    """Liveness check."""
    return {"status": "ok"}


# --- Weather + AQI alert scheduler -------------------------------------------
#
# The repo has no external cron — the server is always-on, so a lightweight
# asyncio loop checks the ET clock every minute. Silence by default: normal
# weather sends nothing. All failures are logged and never messaged.

WEATHER_AQI_HOURS = (8, 10, 12, 14, 16, 18, 20)  # every 2h, 8 AM-8 PM ET
WEATHER_MORNING_HOUR = 7


def _weather_alerts_enabled() -> bool:
    return os.environ.get("WEATHER_ALERTS_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _weather_send(message: str) -> None:
    """Deliver an alert over Telegram; a failure is logged, never surfaced."""
    try:
        _tg_send_chunked(message)
    except Exception as exc:  # noqa: BLE001 — fail silently per design
        log.error("Weather alert send failed: %s", exc)


def _weather_tick(now: Optional[datetime] = None) -> None:
    """One scheduler pass: 7 AM morning check + 2-hourly AQI check."""
    now = now or datetime.now(LOCAL_TZ)
    today = now.date().isoformat()
    ws = store.get_weather_state()

    if now.hour == WEATHER_MORNING_HOUR and ws.get("morning_date") != today:
        ws["morning_date"] = today  # one attempt per day, sent or silent
        store.set_weather_state(ws)
        message = weather.build_morning_alert(now)
        if message:
            log.info("Morning weather alert firing")
            _weather_send(message)
        else:
            log.info("Morning weather check: all clear, staying silent")

    if now.hour in WEATHER_AQI_HOURS:
        run_key = f"{today}T{now.hour:02d}"
        if ws.get("last_aqi_run") != run_key:
            ws["last_aqi_run"] = run_key
            aqi = weather.fetch_current_aqi(now)
            if aqi is not None:
                message, aqi_state = weather.evaluate_aqi(aqi, ws.get("aqi", {}), today)
                ws["aqi"] = aqi_state
                if message:
                    log.info("AQI alert firing (aqi=%s)", aqi)
                    _weather_send(message)
            store.set_weather_state(ws)


async def _weather_loop() -> None:
    log.info("Weather alert scheduler started (60s tick, tz=%s)", LOCAL_TZ)
    while True:
        await asyncio.sleep(60)  # sleep first: no tick during test startup
        try:
            await asyncio.to_thread(_weather_tick)
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.error("Weather tick failed: %s", exc)


@app.on_event("startup")
async def _start_weather_scheduler() -> None:
    if _weather_alerts_enabled():
        asyncio.create_task(_weather_loop())


def _check_page_token(k: Optional[str]) -> None:
    """Gate the demo page + /latest behind PAGE_TOKEN (your sleep/calendar data).

    If PAGE_TOKEN is unset the pages are open (local dev). When set, both / and
    /latest require ?k=<PAGE_TOKEN>.
    """
    token = os.environ.get("PAGE_TOKEN")
    if not token:
        return
    if not hmac.compare_digest(k or "", token):
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/", response_class=HTMLResponse)
def demo_page(k: Optional[str] = Query(default=None)) -> str:
    """Single-file demo page (fetches /latest client-side). Gated by PAGE_TOKEN."""
    _check_page_token(k)
    return (DEMO_HTML
            .replace("__BOT_URL__", persona.chat_url())
            .replace("__BOT_NAME__", persona.name())
            .replace("__PAGE_TOKEN__", os.environ.get("PAGE_TOKEN", "")))


@app.get("/latest")
def latest(k: Optional[str] = Query(default=None)) -> dict:
    """The last brief + parsed sleep summary (for the demo page). Gated by PAGE_TOKEN."""
    _check_page_token(k)
    snapshot = store.load_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="no brief generated yet")
    return snapshot


# --- Telegram conversational webhook ----------------------------------------


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _events_from_snapshot(snapshot: Optional[dict]) -> list[Event]:
    """Reconstruct Event objects from a stored snapshot (calendar-fetch fallback)."""
    if not snapshot:
        return []
    return [
        Event(
            title=e["title"],
            start=datetime.fromisoformat(e["start_iso"]),
            end=datetime.fromisoformat(e["end_iso"]),
            is_all_day=e["is_all_day"],
        )
        for e in snapshot.get("events", [])
    ]


def _chat_context(snapshot: Optional[dict]) -> dict:
    """Context for a chat reply: this morning's sleep/brief + what's still ahead."""
    now = _now()
    try:
        events = get_today_events()  # already scoped now..11pm
    except Exception as exc:  # noqa: BLE001 — degrade to this morning's snapshot
        log.error("chat: calendar fetch failed, using snapshot events: %s", exc)
        events = _events_from_snapshot(snapshot)

    # Only *future* free time: clamp each slot's start to now, drop past/too-short.
    free_slots = []
    for s in find_free_slots(events):
        start = max(s.start, now)
        minutes = int((s.end - start).total_seconds() // 60)
        if minutes >= 20:
            free_slots.append({"start": _clock(start), "end": _clock(s.end), "duration_minutes": minutes})
    remaining = [e for e in events if not e.is_all_day and e.end > now]
    return {
        "now": _clock(now),
        "sleep": snapshot.get("sleep") if snapshot else None,
        "this_morning_brief": snapshot.get("brief") if snapshot else None,
        "remaining_events": [
            {"title": e.title, "start": _clock(e.start), "end": _clock(e.end)} for e in remaining
        ],
        "free_slots": free_slots,
    }


def _require_telegram_secret(token: Optional[str]) -> None:
    """Verify Telegram's X-Telegram-Bot-Api-Secret-Token header."""
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        log.warning("TELEGRAM_WEBHOOK_SECRET not set; /telegram is UNVERIFIED")
        return
    if not hmac.compare_digest(token or "", secret):
        log.warning("Rejected /telegram: bad secret token")
        raise HTTPException(status_code=403, detail="forbidden")


def _handle_reply(user_text: str, send) -> None:
    """Background worker: generate a reply, send it, remember the exchange.

    `send` is the channel's plain-text send callable (Telegram or WhatsApp).
    """
    try:
        snapshot = store.load_snapshot()
        context = _chat_context(snapshot)
        conversation = store.get_conversation()
        reply = generate_reply(user_text, context=context, conversation=conversation)
        send(reply)
        store.append_exchange(user_text, reply)
    except Exception as exc:  # noqa: BLE001 — background task; must not crash silently
        log.error("Chat reply failed: %s", exc)
        try:
            send("my brain's a little foggy right now ☁️ try me again in a sec")
        except Exception:  # noqa: BLE001
            pass


def _today_text() -> str:
    """Compact 'today' view: remaining events + open slots."""
    ctx = _chat_context(store.load_snapshot())
    lines = [f"🗓️ today ({ctx['now']})", "", "on the calendar:"]
    if ctx["remaining_events"]:
        lines += [f"• {e['start']}–{e['end']}  {e['title']}" for e in ctx["remaining_events"]]
    else:
        lines.append("• nothing left — you're clear")
    lines += ["", "open slots:"]
    if ctx["free_slots"]:
        lines += [f"• {s['start']}–{s['end']}  ({s['duration_minutes']}m)" for s in ctx["free_slots"]]
    else:
        lines.append("• fully booked")
    return "\n".join(lines)


_BADGE = {"good": "🟢 good", "short": "🟡 short", "fragmented": "🟡 fragmented", "poor": "🔴 poor"}


def _sleep_text() -> str:
    """Last night's numbers with the quality badge."""
    snap = store.load_snapshot()
    if not snap or not snap.get("sleep"):
        return "no sleep data yet — waiting on tonight 🌙"
    s = snap["sleep"]
    badge = _BADGE.get(s["quality_flag"], s["quality_flag"])
    return (
        f"😴 last night — {badge}\n"
        f"{s['total_hours']}h asleep · deep {s['deep_minutes']:.0f}m · REM {s['rem_minutes']:.0f}m\n"
        f"resting HR {s['resting_hr']} · HRV {s['hrv_ms']} ms"
    )


def _demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes", "on")


def _handle_command(cmd: str, send, send_book) -> None:
    """Background worker for /today, /sleep, /reset.

    `send` sends plain text; `send_book(text, slot_label)` sends text with the
    channel's book/skip buttons attached.
    """
    try:
        if cmd == "today":
            send(_today_text())
        elif cmd == "sleep":
            send(_sleep_text())
        elif cmd == "reset":
            if not _demo_mode():
                send("reset is off right now 🌱 (set DEMO_MODE=true to enable it)")
            else:
                seed = demo_seed.seed(_now())  # re-prime the bad-night demo
                send_book(seed["brief"], seed["slot_label"])
        else:
            send("i don't know that one 🌱 but you can just talk to me 💬")
    except Exception as exc:  # noqa: BLE001 — background task
        log.error("Command /%s failed: %s", cmd, exc)


def _handle_callback(cq: dict) -> None:
    """Background worker for the morning inline buttons (book / skip)."""
    data = cq.get("data")
    cq_id = cq.get("id")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    original = msg.get("text", "")
    try:
        if data == "book":
            restore = (store.load_snapshot() or {}).get("restore")
            if not restore:
                answer_callback(cq_id, "hmm, nothing to book 🌱")
                return
            label = restore.get("start_label", "")
            activity = restore.get("activity", "a break")
            if not _calendar_write_enabled():  # respect the flag
                answer_callback(cq_id, "calendar writes are off (demo-safe) 🌱")
                edit_message_text(chat_id, message_id,
                                  original + f"\n\n🌱 would protect {label} for {activity} (writes off)",
                                  reply_markup={"inline_keyboard": []})
                return
            slot = TimeSlot(
                start=datetime.fromisoformat(restore["start_iso"]),
                end=datetime.fromisoformat(restore["end_iso"]),
                duration_minutes=RESTORE_BLOCK_MINUTES,
            )
            create_restore_block(slot, activity)
            store.mark_restore_booked()  # flips the demo page block to solid
            answer_callback(cq_id, "done! protected 20 min for you 🫶")
            edit_message_text(chat_id, message_id,
                              original + f"\n\n✅ protected {label} for {activity} 🌿",
                              reply_markup={"inline_keyboard": []})
        elif data == "skip":
            answer_callback(cq_id, "no worries 💛")
            edit_message_text(chat_id, message_id, original + "\n\n— not today 💛",
                              reply_markup={"inline_keyboard": []})
        else:
            answer_callback(cq_id)
    except Exception as exc:  # noqa: BLE001 — background task
        log.error("Callback handling failed: %s", exc)
        try:
            answer_callback(cq_id, "hmm, that didn't go through 😞")
        except Exception:  # noqa: BLE001
            pass


def _split_brief(text: str) -> list[str]:
    """Split a brief into its chat bubbles (blank-line separated chunks)."""
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return parts or [text]


def _tg_send_chunked(text: str, reply_markup: Optional[dict] = None) -> None:
    """Send each bubble as its own Telegram message; buttons ride the last one."""
    parts = _split_brief(text)
    for part in parts[:-1]:
        send_telegram(part)
    send_telegram(parts[-1], reply_markup)


def _wa_send_chunked(text: str, buttons: Optional[list] = None) -> None:
    """Send each bubble as its own WhatsApp message; buttons ride the last one."""
    parts = _split_brief(text)
    for part in parts[:-1]:
        send_whatsapp(part)
    send_whatsapp(parts[-1], buttons)


def _tg_send_book(text: str, slot_label: str) -> None:
    _tg_send_chunked(text, _book_markup(slot_label))


def _wa_send_book(text: str, slot_label: str) -> None:
    _wa_send_chunked(text, _wa_book_buttons(slot_label))


@app.post("/telegram")
def telegram_webhook(
    background_tasks: BackgroundTasks,
    update: dict = Body(...),
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict:
    """Telegram webhook. Acknowledges immediately; does the work in the background.

    Fast ack + update_id dedupe prevents Telegram's slow-webhook retries from
    producing duplicate replies or double-bookings.
    """
    _require_telegram_secret(x_telegram_bot_api_secret_token)

    update_id = update.get("update_id")
    callback = update.get("callback_query")
    msg = update.get("message") or update.get("edited_message")
    if update_id is None or (callback is None and msg is None):
        return {"ok": True}

    if callback is not None:
        chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id"))
    else:
        chat_id = str((msg.get("chat") or {}).get("id"))
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    if owner and chat_id != str(owner):
        log.info("Ignoring Telegram update from chat %s (not owner)", chat_id)
        return {"ok": True}

    if not store.mark_update_seen(update_id):
        log.info("Duplicate Telegram update %s ignored", update_id)
        return {"ok": True}

    if callback is not None:
        background_tasks.add_task(_handle_callback, callback)
        return {"ok": True}

    text = msg.get("text")
    if not text:
        return {"ok": True}
    if text.startswith("/"):
        cmd = text[1:].split()[0].split("@")[0].lower()
        background_tasks.add_task(_handle_command, cmd, send_telegram, _tg_send_book)
    else:
        background_tasks.add_task(_handle_reply, text, send_telegram)
    return {"ok": True}


# --- WhatsApp conversational webhook (Meta Cloud API) ------------------------


def _wa_book_buttons(slot_label: str) -> list[dict]:
    """WhatsApp quick-reply buttons: book the Restore block, or dismiss it."""
    return [
        {"id": "book", "title": f"🌿 book {slot_label}"},
        {"id": "skip", "title": "not today"},
    ]


def _handle_whatsapp_button(button_id: str) -> None:
    """Background worker for the morning buttons (book / skip).

    WhatsApp can't edit sent messages the way Telegram does, so confirmations
    go out as fresh messages instead.
    """
    try:
        if button_id == "book":
            restore = (store.load_snapshot() or {}).get("restore")
            if not restore:
                send_whatsapp("hmm, nothing to book 🌱")
                return
            label = restore.get("start_label", "")
            activity = restore.get("activity", "a break")
            if not _calendar_write_enabled():  # respect the flag
                send_whatsapp(f"🌱 would protect {label} for {activity} "
                              "(calendar writes are off, demo-safe)")
                return
            slot = TimeSlot(
                start=datetime.fromisoformat(restore["start_iso"]),
                end=datetime.fromisoformat(restore["end_iso"]),
                duration_minutes=RESTORE_BLOCK_MINUTES,
            )
            create_restore_block(slot, activity)
            store.mark_restore_booked()  # flips the demo page block to solid
            send_whatsapp(f"✅ protected {label} for {activity} 🌿")
        elif button_id == "skip":
            send_whatsapp("no worries 💛 — not today")
    except Exception as exc:  # noqa: BLE001 — background task
        log.error("WhatsApp button handling failed: %s", exc)
        try:
            send_whatsapp("hmm, that didn't go through 😞")
        except Exception:  # noqa: BLE001
            pass


def _verify_whatsapp_signature(raw_body: bytes, signature: Optional[str]) -> None:
    """Verify Meta's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not secret:
        log.warning("WHATSAPP_APP_SECRET not set; /whatsapp is UNVERIFIED")
        return
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature or "", expected):
        log.warning("Rejected /whatsapp: bad signature")
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/whatsapp")
def whatsapp_verify(
    mode: Optional[str] = Query(default=None, alias="hub.mode"),
    token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta's one-time webhook verification handshake: echo hub.challenge back."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and expected and hmac.compare_digest(token or "", expected):
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="forbidden")


@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> dict:
    """WhatsApp webhook. Acknowledges immediately; does the work in the background.

    Fast ack + message-id dedupe prevents Meta's slow-webhook retries from
    producing duplicate replies or double-bookings. Delivery/read receipts
    ("statuses") are ignored.
    """
    raw = await request.body()
    _verify_whatsapp_signature(raw, x_hub_signature_256)
    try:
        update = json.loads(raw)
    except ValueError:
        return {"ok": True}

    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            for msg in (change.get("value") or {}).get("messages", []):
                _dispatch_whatsapp_message(msg, background_tasks)
    return {"ok": True}


def _dispatch_whatsapp_message(msg: dict, background_tasks: BackgroundTasks) -> None:
    owner = os.environ.get("WHATSAPP_TO", "").lstrip("+")
    sender = (msg.get("from") or "").lstrip("+")
    if owner and sender != owner:
        log.info("Ignoring WhatsApp message from %s (not owner)", sender)
        return
    if not store.mark_update_seen(msg.get("id")):
        log.info("Duplicate WhatsApp message %s ignored", msg.get("id"))
        return

    if msg.get("type") == "interactive":
        reply = (msg.get("interactive") or {}).get("button_reply") or {}
        background_tasks.add_task(_handle_whatsapp_button, reply.get("id"))
        return
    text = (msg.get("text") or {}).get("body")
    if not text:
        return
    if text.startswith("/"):
        cmd = text[1:].split()[0].lower()
        background_tasks.add_task(_handle_command, cmd, send_whatsapp, _wa_send_book)
    else:
        background_tasks.add_task(_handle_reply, text, send_whatsapp)


def _fallback_brief(sleep: SleepSummary) -> str:
    """Companion-voice brief from the raw numbers, used when Claude is down."""
    return (
        f"my brain's a little foggy this morning ☁️ but here's last night: "
        f"{sleep.total_hours}h sleep ({sleep.quality_flag}), "
        f"deep {sleep.deep_minutes:.0f}m, REM {sleep.rem_minutes:.0f}m, "
        f"resting HR {sleep.resting_hr}. go easy today 💛"
    )


def _push_title(sleep: SleepSummary) -> str:
    return f"AnAn — {sleep.quality_flag} night"


def _book_markup(slot_label: str) -> dict:
    """Inline keyboard: book the proposed Restore block, or dismiss it."""
    return {"inline_keyboard": [[
        {"text": f"🌿 book it for {slot_label}", "callback_data": "book"},
        {"text": "not today", "callback_data": "skip"},
    ]]}


def _deliver(title: str, message: str, reply_markup: Optional[dict] = None,
             slot_label: Optional[str] = None) -> dict:
    """Send the brief over the configured channel(s). Never raises — each
    channel's failure is logged and reported so /wake always returns.

    PUSH_CHANNEL: "telegram" (default), "whatsapp", "ntfy", "both"
    (= telegram+ntfy), or any comma-separated mix, e.g. "whatsapp,ntfy".
    """
    raw = os.environ.get("PUSH_CHANNEL", "telegram").strip().lower()
    channels = {"telegram", "ntfy"} if raw == "both" else {
        c.strip() for c in raw.split(",") if c.strip()
    }
    delivered: dict = {}
    if "telegram" in channels:
        try:
            _tg_send_chunked(message, reply_markup)
            delivered["telegram"] = True
        except Exception as exc:  # noqa: BLE001 — resilience path; logged
            log.error("Telegram send failed: %s", exc)
            delivered["telegram"] = False
    if "whatsapp" in channels:
        try:
            buttons = _wa_book_buttons(slot_label) if slot_label else None
            _wa_send_chunked(message, buttons)
            delivered["whatsapp"] = True
        except Exception as exc:  # noqa: BLE001 — resilience path; logged
            log.error("WhatsApp send failed: %s", exc)
            delivered["whatsapp"] = False
    if "ntfy" in channels:
        try:
            send_push(title, message)
            delivered["ntfy"] = True
        except Exception as exc:  # noqa: BLE001 — resilience path; logged
            log.error("ntfy push failed: %s", exc)
            delivered["ntfy"] = False
    if not delivered:
        log.warning("Unknown PUSH_CHANNEL=%r; nothing delivered", raw)
    return delivered


def _require_webhook_auth(authorization: Optional[str]) -> None:
    """Reject the request unless it carries `Authorization: Bearer <WEBHOOK_SECRET>`.

    If WEBHOOK_SECRET is unset (local dev), the endpoint is open — but we log a
    loud warning so it's never a silent hole in production.
    """
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        log.warning("WEBHOOK_SECRET not set; /wake is UNAUTHENTICATED")
        return
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(authorization or "", expected):
        log.warning("Rejected /wake: bad or missing bearer token")
        raise HTTPException(status_code=401, detail="unauthorized")


def _calendar_write_enabled() -> bool:
    return os.environ.get("CALENDAR_WRITE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _match_slot(start: datetime, free_slots: list):
    """Return the free slot whose start equals `start`, or None. Never trust the
    model's proposed time — it must correspond to a slot we actually offered."""
    return next((s for s in free_slots if s.start == start), None)


@app.post("/wake")
def wake(
    payload: dict = Body(...),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Run the morning flow. Fails loudly only on an unparseable payload."""
    _require_webhook_auth(authorization)
    log.info("POST /wake received (%d top-level key(s))", len(payload))

    # Sleep parsing is the one hard requirement — no sleep data, no brief.
    try:
        sleep = parse_sleep(payload)
    except (ValidationError, ValueError) as exc:
        log.error("Failed to parse /wake payload: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Calendar is best-effort: on failure, degrade to a sleep-only brief.
    calendar_ok = True
    try:
        events = get_today_events()
    except Exception as exc:  # noqa: BLE001 — resilience path; failure is logged
        log.error("Calendar fetch failed; continuing sleep-only: %s", exc)
        events = []
        calendar_ok = False
    free_slots = find_free_slots(events)

    # Brief is best-effort: on failure, fall back to the raw sleep summary.
    brief_source = "claude"
    proposed = None
    try:
        result = generate_brief(sleep, events, free_slots)
        brief = result.brief
        proposed = result.restore_block
    except Exception as exc:  # noqa: BLE001 — resilience path; failure is logged
        log.error("Claude brief generation failed; using fallback: %s", exc)
        brief = _fallback_brief(sleep)
        brief_source = "fallback"

    # Stretch: PROPOSE a recovery block (validated). We no longer auto-write —
    # the user books it with the Telegram button, which respects the write flag.
    restore_proposed = None
    restore_bookable = False
    markup = None
    slot_label = None
    if proposed is not None:
        restore_proposed = {"start": proposed.start.isoformat(), "activity": proposed.activity}
        slot = _match_slot(proposed.start, free_slots)
        if sleep.quality_flag in RESTORE_FLAGS and slot is not None:
            restore_bookable = True
            slot_label = _clock(slot.start)
            markup = _book_markup(slot_label)
        else:
            log.info(
                "Restore proposal not bookable (quality=%s, slot_matched=%s)",
                sleep.quality_flag, slot is not None,
            )

    # Delivery is best-effort: a failure still returns the brief in the response.
    delivered = _deliver(_push_title(sleep), brief, markup, slot_label)

    # Persist a snapshot for /latest and the demo page (block starts un-booked).
    restore_for_snapshot = None
    if proposed is not None:
        restore_for_snapshot = {
            "start": proposed.start,
            "activity": proposed.activity,
            "created": False,
        }
    store.save_snapshot(store.build_snapshot(
        sleep=sleep,
        events=events,
        brief=brief,
        brief_source=brief_source,
        restore=restore_for_snapshot,
        generated_at=datetime.now(LOCAL_TZ),
    ))

    log.info(
        "Wake done: quality=%s calendar_ok=%s brief_source=%s delivered=%s bookable=%s",
        sleep.quality_flag, calendar_ok, brief_source, delivered, restore_bookable,
    )
    return {
        "brief": brief,
        "brief_source": brief_source,
        "calendar_ok": calendar_ok,
        "delivered": delivered,
        "calendar_write_enabled": _calendar_write_enabled(),
        "restore_proposed": restore_proposed,
        "restore_bookable": restore_bookable,
        "restore_created": None,  # booked later via the Telegram button
        "sleep": sleep.model_dump(mode="json"),
    }
