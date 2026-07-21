"""Tests for sleep parsing and the /wake + /health endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app
from app.sleep import parse_sleep

FIXTURES = Path(__file__).parent / "fixtures"
ET = ZoneInfo("America/New_York")


def _et(hh: int, mm: int) -> datetime:
    return datetime(2026, 7, 16, hh, mm, tzinfo=ET)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def good_night() -> dict:
    return load_fixture("sleep_good_night.json")


@pytest.fixture
def bad_night() -> dict:
    return load_fixture("sleep_bad_night.json")


client = TestClient(app)


# --- parse_sleep ------------------------------------------------------------


def test_parse_good_night(good_night):
    s = parse_sleep(good_night)
    assert s.quality_flag == "good"
    assert s.total_hours == pytest.approx(7.3)
    assert s.deep_minutes == pytest.approx(78.0)   # 1.3h * 60
    assert s.rem_minutes == pytest.approx(108.0)   # 1.8h * 60
    assert s.awake_minutes == pytest.approx(21.0)  # 0.35h * 60
    assert s.resting_hr == pytest.approx(52.0)
    assert s.hrv_ms == pytest.approx(68.0)
    # timestamps parsed with timezone
    assert s.sleep_start.isoformat() == "2026-07-15T23:02:00+00:00"
    assert s.sleep_end.isoformat() == "2026-07-16T06:45:00+00:00"


def test_parse_bad_night(bad_night):
    s = parse_sleep(bad_night)
    # ~5h with only 36 min deep and resting HR 66 -> worst tier
    assert s.quality_flag == "poor"
    assert s.total_hours == pytest.approx(5.0)
    assert s.deep_minutes == pytest.approx(36.0)   # 0.6h * 60
    assert s.rem_minutes == pytest.approx(42.0)    # 0.7h * 60
    assert s.awake_minutes == pytest.approx(51.0)  # 0.85h * 60
    assert s.resting_hr == pytest.approx(66.0)
    assert s.hrv_ms == pytest.approx(34.0)


def test_parse_missing_sleep_metric_raises():
    payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"qty": 1}]}]}}
    with pytest.raises(ValueError, match="sleep_analysis"):
        parse_sleep(payload)


# --- endpoints --------------------------------------------------------------


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


from app.advisor import BriefResult, RestoreBlock  # noqa: E402


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub the external legs of /wake (calendar, Claude, delivery) — no network.

    Returns a dict recording deliveries per channel so tests can assert on it.
    """
    from app import main

    calls = {"telegram": [], "ntfy": []}

    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": [])
    monkeypatch.setattr(main, "generate_brief", lambda s, e, f: BriefResult(brief="STUB BRIEF"))
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: calls["telegram"].append(message))
    monkeypatch.setattr(main, "send_push", lambda title, message: calls["ntfy"].append((title, message)))
    return calls


def test_wake_good_night(good_night, stub_pipeline):
    resp = client.post("/wake", json=good_night)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sleep"]["quality_flag"] == "good"
    assert body["brief"] == "STUB BRIEF"
    assert body["brief_source"] == "claude"
    assert body["calendar_ok"] is True
    assert body["delivered"] == {"telegram": True}  # default channel
    assert stub_pipeline["telegram"] == ["STUB BRIEF"]
    assert stub_pipeline["ntfy"] == []


def test_wake_bad_night(bad_night, stub_pipeline):
    resp = client.post("/wake", json=bad_night)
    assert resp.status_code == 200
    assert resp.json()["sleep"]["quality_flag"] == "poor"


def test_wake_bad_payload_returns_422():
    resp = client.post("/wake", json={"data": {"metrics": []}})
    assert resp.status_code == 422


# --- webhook bearer auth ----------------------------------------------------


def test_wake_rejects_missing_bearer_when_secret_set(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    resp = client.post("/wake", json=bad_night)  # no Authorization header
    assert resp.status_code == 401


def test_wake_rejects_wrong_bearer(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    resp = client.post("/wake", json=bad_night, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_wake_accepts_correct_bearer(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    resp = client.post("/wake", json=bad_night, headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200


def test_wake_open_when_secret_unset(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    resp = client.post("/wake", json=bad_night)  # unauthenticated dev mode
    assert resp.status_code == 200


# --- demo endpoints: / and /latest ------------------------------------------


def test_root_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "AnAn" in resp.text


def test_latest_404_when_no_snapshot():
    assert client.get("/latest").status_code == 404  # fresh isolated state


def test_page_gate_blocks_without_token(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("PAGE_TOKEN", "sekret")
    client.post("/wake", json=bad_night)  # seed a snapshot
    assert client.get("/latest").status_code == 403          # no ?k
    assert client.get("/latest?k=wrong").status_code == 403  # wrong ?k
    assert client.get("/").status_code == 403                # page gated too
    assert client.get("/latest?k=sekret").status_code == 200 # correct ?k
    r = client.get("/?k=sekret")
    assert r.status_code == 200 and "sekret" in r.text       # token injected for the page's fetch


def test_wake_writes_snapshot_then_latest_returns_it(bad_night, stub_pipeline):
    assert client.post("/wake", json=bad_night).status_code == 200

    snap = client.get("/latest")
    assert snap.status_code == 200
    body = snap.json()
    assert body["brief"] == "STUB BRIEF"
    assert body["sleep"]["quality_flag"] == "poor"
    # derived core sleep = total(300m) - deep(36) - rem(42) = 222
    assert body["sleep"]["core_minutes"] == 222.0


# --- Telegram conversational webhook ----------------------------------------


def _tg_update(update_id, text, chat_id=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_telegram_requires_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    upd = _tg_update(1, "hi")
    assert client.post("/telegram", json=upd).status_code == 403                       # missing
    assert client.post("/telegram", json=upd,
                       headers={"X-Telegram-Bot-Api-Secret-Token": "bad"}).status_code == 403


def test_telegram_ignores_other_chat(monkeypatch):
    from app import main
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    called = []
    monkeypatch.setattr(main, "_handle_reply", lambda text, send: called.append(text))
    r = client.post("/telegram", json=_tg_update(2, "hi", chat_id=999),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wh"})
    assert r.status_code == 200 and called == []


def test_telegram_dedupes_update_id(monkeypatch):
    from app import main
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    called = []
    monkeypatch.setattr(main, "_handle_reply", lambda text, send: called.append(text))
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "wh"}
    client.post("/telegram", json=_tg_update(7, "hi"), headers=hdr)
    client.post("/telegram", json=_tg_update(7, "hi"), headers=hdr)  # retry, same id
    assert called == ["hi"]  # processed exactly once


def test_telegram_reply_uses_a_real_free_slot(monkeypatch, bad_night):
    from app import main
    from app.calendar_client import Event

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(main, "_now", lambda: _et(8, 0))  # fix "now" so slots are deterministic

    # Seed the morning snapshot via /wake (stubbed pipeline).
    day = [Event(title="Client meeting", start=_et(14, 0), end=_et(15, 0), is_all_day=False)]
    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": day)
    monkeypatch.setattr(main, "generate_brief", lambda s, e, f: BriefResult(brief="morning 🌙"))
    sent = []
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.append(message))
    client.post("/wake", json=bad_night)

    # The reply function is stubbed to actually use a free slot from the context.
    captured = {}

    def fake_reply(user_text, *, context, conversation, **kw):
        captured["context"] = context
        captured["conversation"] = conversation
        slot = context["free_slots"][0]
        return f"you've got a gap at {slot['start']} — nap then 🌙"

    monkeypatch.setattr(main, "generate_reply", fake_reply)

    r = client.post("/telegram", json=_tg_update(9, "when should I nap?"),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wh"})
    assert r.status_code == 200

    assert captured["context"]["free_slots"], "chat context should carry real free slots"
    assert captured["conversation"] == []  # fresh window after this morning's /wake
    assert sent[-1].startswith("you've got a gap at")
    # the reply names a real slot time from today's calendar
    assert captured["context"]["free_slots"][0]["start"] in sent[-1]

    # the exchange is remembered
    conv = store.get_conversation()
    assert conv[-2] == {"role": "user", "text": "when should I nap?"}
    assert conv[-1]["role"] == "assistant"


# --- /wake resilience -------------------------------------------------------


def test_wake_survives_calendar_failure(monkeypatch, bad_night):
    from app import main

    def boom(calendar_id="primary"):
        raise RuntimeError("no token")

    seen = {}
    monkeypatch.setattr(main, "get_today_events", boom)

    def capture(s, e, f):
        seen["events"] = e
        return BriefResult(brief="OK")

    monkeypatch.setattr(main, "generate_brief", capture)
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: None)

    resp = client.post("/wake", json=bad_night)
    assert resp.status_code == 200
    body = resp.json()
    assert body["calendar_ok"] is False
    assert body["brief"] == "OK"
    assert seen["events"] == []  # degraded to sleep-only


def test_wake_falls_back_when_claude_fails(monkeypatch, bad_night):
    from app import main

    sent = {}
    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": [])
    monkeypatch.setattr(main, "generate_brief", lambda s, e, f: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.update(message=message))

    resp = client.post("/wake", json=bad_night)
    assert resp.status_code == 200
    body = resp.json()
    assert body["brief_source"] == "fallback"
    assert "foggy" in body["brief"]                  # companion-voice fallback
    assert "5.0h sleep (poor)" in body["brief"]      # raw numbers made it in
    assert sent["message"] == body["brief"]          # fallback got delivered


def test_wake_reports_delivery_failure(monkeypatch, bad_night):
    from app import main

    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": [])
    monkeypatch.setattr(main, "generate_brief", lambda s, e, f: BriefResult(brief="OK"))
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: (_ for _ in ()).throw(RuntimeError("tg 500")))

    resp = client.post("/wake", json=bad_night)
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivered"]["telegram"] is False    # send failed...
    assert body["brief"] == "OK"                      # ...but we still return the brief


def test_push_channel_both_uses_both(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("PUSH_CHANNEL", "both")
    body = client.post("/wake", json=bad_night).json()
    assert body["delivered"] == {"telegram": True, "ntfy": True}
    assert stub_pipeline["telegram"] == ["STUB BRIEF"]
    assert stub_pipeline["ntfy"] == [("AnAn — poor night", "STUB BRIEF")]


def test_push_channel_ntfy_only(monkeypatch, bad_night, stub_pipeline):
    monkeypatch.setenv("PUSH_CHANNEL", "ntfy")
    body = client.post("/wake", json=bad_night).json()
    assert body["delivered"] == {"ntfy": True}
    assert stub_pipeline["telegram"] == []


# --- stretch: restore-block calendar write ---------------------------------


def _first_free_slot(events):
    from app.calendar_client import find_free_slots
    return find_free_slots(events)[0]


def test_wake_proposes_button_but_does_not_write(monkeypatch, bad_night):
    from app import main
    from app.calendar_client import Event

    day = [Event(title="Class", start=_et(9, 30), end=_et(10, 45), is_all_day=False)]
    slot = _first_free_slot(day)
    sent = {}
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": day)
    monkeypatch.setattr(
        main, "generate_brief",
        lambda s, e, f: BriefResult(brief="rough night",
                                    restore_block=RestoreBlock(start=slot.start, activity="walk outside")),
    )
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.update(markup=markup))
    monkeypatch.setattr(main, "create_restore_block",
                        lambda s, a: pytest.fail("/wake must not write — booking is button-driven"))

    body = client.post("/wake", json=bad_night).json()
    assert body["restore_bookable"] is True
    assert body["restore_created"] is None
    assert body["restore_proposed"]["activity"] == "walk outside"
    assert sent["markup"]["inline_keyboard"][0][0]["callback_data"] == "book"   # button attached
    assert client.get("/latest").json()["restore"]["created"] is False          # dashed on the page


def test_wake_no_button_when_time_not_a_real_slot(monkeypatch, bad_night):
    from app import main
    from app.calendar_client import Event

    day = [Event(title="Class", start=_et(9, 30), end=_et(10, 45), is_all_day=False)]
    sent = {}
    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": day)
    monkeypatch.setattr(
        main, "generate_brief",
        lambda s, e, f: BriefResult(brief="rough", restore_block=RestoreBlock(start=_et(3, 0), activity="nap")),
    )
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.update(markup=markup))
    body = client.post("/wake", json=bad_night).json()
    assert body["restore_bookable"] is False
    assert sent["markup"] is None


def test_wake_no_button_for_good_night(monkeypatch, good_night):
    from app import main
    sent = {}
    monkeypatch.setattr(main, "get_today_events", lambda calendar_id="primary": [])
    monkeypatch.setattr(main, "generate_brief",
                        lambda s, e, f: BriefResult(brief="well rested", restore_block=None))
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.update(markup=markup))
    body = client.post("/wake", json=good_night).json()
    assert body["restore_bookable"] is False
    assert body["restore_proposed"] is None
    assert sent["markup"] is None


# --- inline button (callback_query) booking flow ----------------------------


def _callback(update_id, data, chat_id=42):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data,
        "message": {"message_id": 555, "chat": {"id": chat_id}, "text": "morning brief"}}}


def _seed_and_headers(monkeypatch):
    from app import demo_seed
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    demo_seed.seed(_et(8, 0))  # snapshot with an un-booked restore block
    return {"X-Telegram-Bot-Api-Secret-Token": "wh"}


def test_callback_book_writes_and_flips_snapshot(monkeypatch):
    from app import main
    hdr = _seed_and_headers(monkeypatch)
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    booked, answered, edited = {}, {}, {}
    monkeypatch.setattr(main, "create_restore_block",
                        lambda slot, activity: booked.update(slot=slot, activity=activity) or {"summary": "x"})
    monkeypatch.setattr(main, "answer_callback", lambda cid, text=None: answered.update(text=text))
    monkeypatch.setattr(main, "edit_message_text",
                        lambda chat, mid, text, reply_markup=None: edited.update(text=text, markup=reply_markup))

    r = client.post("/telegram", json=_callback(11, "book"), headers=hdr)
    assert r.status_code == 200
    assert booked["activity"] == "walk outside"                 # calendar write happened
    assert "protected" in answered["text"]                       # cute confirmation toast
    assert "✅" in edited["text"] and edited["markup"] == {"inline_keyboard": []}  # message edited, buttons gone
    assert client.get("/latest").json()["restore"]["created"] is True             # page flips to solid


def test_callback_book_respects_write_disabled(monkeypatch):
    from app import main
    hdr = _seed_and_headers(monkeypatch)
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "false")
    monkeypatch.setattr(main, "create_restore_block",
                        lambda slot, activity: pytest.fail("must not write when disabled"))
    monkeypatch.setattr(main, "answer_callback", lambda cid, text=None: None)
    monkeypatch.setattr(main, "edit_message_text", lambda *a, **k: None)
    client.post("/telegram", json=_callback(12, "book"), headers=hdr)
    assert client.get("/latest").json()["restore"]["created"] is False


def test_callback_skip_does_not_write(monkeypatch):
    from app import main
    hdr = _seed_and_headers(monkeypatch)
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    answered = {}
    monkeypatch.setattr(main, "create_restore_block",
                        lambda slot, activity: pytest.fail("skip must not write"))
    monkeypatch.setattr(main, "answer_callback", lambda cid, text=None: answered.update(text=text))
    monkeypatch.setattr(main, "edit_message_text", lambda *a, **k: None)
    client.post("/telegram", json=_callback(13, "skip"), headers=hdr)
    assert client.get("/latest").json()["restore"]["created"] is False
    assert "no worries" in answered["text"]


# --- commands: /today /sleep /reset -----------------------------------------


def test_commands_today_and_sleep(monkeypatch, bad_night):
    from app import main
    from app.calendar_client import Event
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(main, "_now", lambda: _et(8, 0))
    monkeypatch.setattr(main, "get_today_events",
                        lambda calendar_id="primary": [Event(title="Client meeting",
                                                             start=_et(14, 0), end=_et(15, 0), is_all_day=False)])
    monkeypatch.setattr(main, "generate_brief", lambda s, e, f: BriefResult(brief="morning"))
    sent = []
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.append(message))
    client.post("/wake", json=bad_night)  # seed the sleep snapshot
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "wh"}

    client.post("/telegram", json=_tg_update(21, "/today"), headers=hdr)
    assert "open slots" in sent[-1] and "Client meeting" in sent[-1]
    client.post("/telegram", json=_tg_update(22, "/sleep@RestoreBot"), headers=hdr)  # @mention stripped
    assert "last night" in sent[-1] and "poor" in sent[-1]


def test_reset_command_gated_by_demo_mode(monkeypatch):
    from app import main
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(main, "_now", lambda: _et(8, 0))
    sent = []
    monkeypatch.setattr(main, "send_telegram", lambda message, markup=None: sent.append((message, markup)))
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "wh"}

    monkeypatch.setenv("DEMO_MODE", "false")
    client.post("/telegram", json=_tg_update(31, "/reset"), headers=hdr)
    assert "off" in sent[-1][0] and sent[-1][1] is None       # refused, no morning message

    monkeypatch.setenv("DEMO_MODE", "true")
    client.post("/telegram", json=_tg_update(32, "/reset"), headers=hdr)
    assert sent[-1][1]["inline_keyboard"][0][0]["callback_data"] == "book"   # morning msg + button
    assert client.get("/latest").json()["sleep"]["quality_flag"] == "poor"


def test_chat_context_clamps_past_free_slots(monkeypatch):
    """A slot spanning 'now' must be reported starting at now, never in the past."""
    from app import main
    from app.calendar_client import Event
    monkeypatch.setattr(main, "_now", lambda: _et(14, 0))  # 2pm
    monkeypatch.setattr(main, "get_today_events",
                        lambda: [Event(title="Workout", start=_et(18, 0), end=_et(19, 0), is_all_day=False)])
    ctx = main._chat_context({})
    assert ctx["free_slots"], "should still surface afternoon/evening slots"
    assert all(s["start"].endswith("PM") for s in ctx["free_slots"])  # nothing before 2pm
