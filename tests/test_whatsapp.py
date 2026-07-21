"""WhatsApp channel: client payloads, webhook verification, and dispatch.

All network calls are stubbed — no live Meta API traffic.
"""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import whatsapp_client
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def whatsapp_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "111222333")
    monkeypatch.setenv("WHATSAPP_TO", "15551234567")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)


def _message_update(msg: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


def _text_msg(body: str, wamid: str = "wamid.1", sender: str = "15551234567") -> dict:
    return {"from": sender, "id": wamid, "type": "text", "text": {"body": body}}


# --- client payloads ---------------------------------------------------------


def test_send_message_plain_text(monkeypatch):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["payload"] = json
        sent["headers"] = headers

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"messages": [{"id": "wamid.out"}]}
        return R()

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    whatsapp_client.send_message("good morning 🌿")
    assert sent["url"].endswith("/111222333/messages")
    assert sent["headers"]["Authorization"] == "Bearer test-token"
    assert sent["payload"]["to"] == "15551234567"
    assert sent["payload"]["type"] == "text"
    assert sent["payload"]["text"]["body"] == "good morning 🌿"


def test_send_message_with_buttons_truncates_titles(monkeypatch):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["payload"] = json

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {}
        return R()

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    whatsapp_client.send_message("brief", buttons=[
        {"id": "book", "title": "🌿 book it for 10:30 AM sharp"},
        {"id": "skip", "title": "not today"},
    ])
    interactive = sent["payload"]["interactive"]
    buttons = interactive["action"]["buttons"]
    assert sent["payload"]["type"] == "interactive"
    assert interactive["body"]["text"] == "brief"
    assert len(buttons[0]["reply"]["title"]) <= whatsapp_client.MAX_BUTTON_TITLE
    assert buttons[1]["reply"] == {"id": "skip", "title": "not today"}


def test_outside_window_sends_template_nudge(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEMPLATE_NAME", "anan_nudge")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        status = 400 if json["type"] == "text" else 200

        class R:
            status_code = status
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError("should have raised OutsideWindowError first")
            def json(self):
                return {"error": {"code": whatsapp_client.OUTSIDE_WINDOW_CODE,
                                  "message": "window closed"}}
        return R()

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    with pytest.raises(whatsapp_client.OutsideWindowError):
        whatsapp_client.send_message("brief")
    assert calls[1]["type"] == "template"
    assert calls[1]["template"]["name"] == "anan_nudge"


# --- webhook verification handshake ------------------------------------------


def test_verify_handshake_echoes_challenge():
    resp = client.get("/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me",
        "hub.challenge": "12345",
    })
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_verify_handshake_rejects_bad_token():
    resp = client.get("/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong",
        "hub.challenge": "12345",
    })
    assert resp.status_code == 403


# --- webhook dispatch ---------------------------------------------------------


def test_chat_message_generates_reply(monkeypatch):
    sent, replies = [], []
    monkeypatch.setattr(main, "send_whatsapp", lambda text, buttons=None: sent.append(text))
    monkeypatch.setattr(main, "generate_reply",
                        lambda text, context, conversation: replies.append(text) or "hey 💛")
    monkeypatch.setattr(main, "get_today_events", lambda: [])

    resp = client.post("/whatsapp", json=_message_update(_text_msg("how's my day?")))
    assert resp.status_code == 200
    assert replies == ["how's my day?"]
    assert sent == ["hey 💛"]


def test_duplicate_wamid_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_whatsapp", lambda text, buttons=None: sent.append(text))
    monkeypatch.setattr(main, "generate_reply", lambda text, context, conversation: "hey")
    monkeypatch.setattr(main, "get_today_events", lambda: [])

    update = _message_update(_text_msg("hi", wamid="wamid.dup"))
    client.post("/whatsapp", json=update)
    client.post("/whatsapp", json=update)
    assert len(sent) == 1


def test_non_owner_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_whatsapp", lambda text, buttons=None: sent.append(text))
    monkeypatch.setattr(main, "generate_reply", lambda text, context, conversation: "hey")

    resp = client.post("/whatsapp", json=_message_update(
        _text_msg("hi", sender="19998887777")))
    assert resp.status_code == 200
    assert sent == []


def test_statuses_only_update_is_acked():
    resp = client.post("/whatsapp", json={
        "entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]
    })
    assert resp.status_code == 200


def test_book_button_respects_write_flag(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_whatsapp", lambda text, buttons=None: sent.append(text))
    monkeypatch.setattr(main.store, "load_snapshot", lambda: {
        "restore": {"start_label": "3:00 PM", "activity": "a walk",
                    "start_iso": "2026-07-20T15:00:00-04:00",
                    "end_iso": "2026-07-20T15:20:00-04:00"},
    })
    monkeypatch.delenv("CALENDAR_WRITE_ENABLED", raising=False)

    resp = client.post("/whatsapp", json=_message_update({
        "from": "15551234567", "id": "wamid.btn", "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "book", "title": "🌿 book 3:00 PM"}},
    }))
    assert resp.status_code == 200
    assert len(sent) == 1
    assert "3:00 PM" in sent[0] and "writes are off" in sent[0]


def test_command_routes_to_shared_handler(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_whatsapp", lambda text, buttons=None: sent.append(text))
    monkeypatch.setattr(main, "get_today_events", lambda: [])

    resp = client.post("/whatsapp", json=_message_update(_text_msg("/sleep")))
    assert resp.status_code == 200
    assert sent == ["no sleep data yet — waiting on tonight 🌙"]


def test_signature_required_when_secret_set(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    body = json.dumps(_message_update(_text_msg("hi"))).encode()

    resp = client.post("/whatsapp", content=body,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": "sha256=bogus"})
    assert resp.status_code == 403

    import hashlib, hmac as hmac_mod
    good = "sha256=" + hmac_mod.new(b"app-secret", body, hashlib.sha256).hexdigest()
    resp = client.post("/whatsapp", content=body,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": good})
    assert resp.status_code == 200
