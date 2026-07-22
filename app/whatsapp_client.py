"""Send messages via the Meta WhatsApp Cloud API.

Plain httpx against the Graph API. Free-form text works only inside WhatsApp's
24-hour customer-service window (it opens/refreshes every time YOU message the
bot). Outside the window Meta rejects the send with error 131047; if
WHATSAPP_TEMPLATE_NAME is set we then send that pre-approved template as a
"good morning, message me for your brief" nudge — replying reopens the window.

Reads WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TO from the
environment.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# WhatsApp hard limits: 3 buttons max, 20 chars per button title, 1024 chars
# for an interactive message body.
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_INTERACTIVE_BODY = 1024

OUTSIDE_WINDOW_CODE = 131047  # "Re-engagement message" — 24h window closed


class OutsideWindowError(RuntimeError):
    """Free-form send rejected because the 24-hour service window is closed."""


def _access_token() -> str:
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN not set")
    return token


def _messages_url() -> str:
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID not set")
    return f"{GRAPH_BASE}/{phone_id}/messages"


def _recipient() -> str:
    to = os.environ.get("WHATSAPP_TO")
    if not to:
        raise RuntimeError("WHATSAPP_TO not set")
    return to.lstrip("+")


def _post(payload: dict) -> dict:
    payload = {"messaging_product": "whatsapp", "to": _recipient(), **payload}
    resp = httpx.post(
        _messages_url(),
        json=payload,
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=10.0,
    )
    if resp.status_code >= 400:
        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        if error.get("code") == OUTSIDE_WINDOW_CODE:
            raise OutsideWindowError(error.get("message", "24h window closed"))
        log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def _button_payload(text: str, buttons: list[dict]) -> dict:
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {
                    "id": b["id"], "title": b["title"][:MAX_BUTTON_TITLE],
                }}
                for b in buttons[:MAX_BUTTONS]
            ]},
        },
    }


def send_message(text: str, buttons: Optional[list[dict]] = None) -> dict:
    """Send `text` to the configured number, optionally with reply buttons.

    `buttons` is a list of {"id": ..., "title": ...} dicts. If the text is too
    long for an interactive body, the text goes out as a plain message and the
    buttons follow in a short second message.

    Raises OutsideWindowError (after sending the template nudge, if one is
    configured) when the 24-hour window is closed.
    """
    log.info("POST WhatsApp send (%d chars, buttons=%s)", len(text), bool(buttons))
    try:
        if buttons and len(text) <= MAX_INTERACTIVE_BODY:
            return _post(_button_payload(text, buttons))
        result = _post({"type": "text", "text": {"body": text}})
        if buttons:
            _post(_button_payload("want me to hold that slot? 🌿", buttons))
        return result
    except OutsideWindowError:
        _send_window_nudge()
        raise


def _send_window_nudge() -> None:
    """Send the pre-approved template so the user can reply and reopen the window."""
    template = os.environ.get("WHATSAPP_TEMPLATE_NAME")
    if not template:
        log.warning("24h window closed and WHATSAPP_TEMPLATE_NAME not set; "
                    "brief not delivered")
        return
    lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en_US")
    try:
        _post({"type": "template",
               "template": {"name": template, "language": {"code": lang}}})
        log.info("24h window closed; sent template nudge %r", template)
    except Exception as exc:  # noqa: BLE001 — best-effort fallback; logged
        log.error("Template nudge failed: %s", exc)
