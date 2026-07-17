"""Send/edit messages and answer callbacks via a Telegram bot.

Plain httpx against the Bot API. We send message text as PLAIN TEXT (no
parse_mode) on purpose: the advisor writes chat-style text with line breaks and
emoji, and any Markdown/HTML parse mode would choke on stray characters.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    return token


def send_message(text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
    """Send `text` to the configured chat, optionally with an inline keyboard.

    Returns the sent message object (has message_id) on success. Raises if
    unconfigured or on HTTP failure.
    """
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")

    payload: dict = {"chat_id": chat_id, "text": text}  # no parse_mode → plain text
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    url = f"{API_BASE}/bot{_token()}/sendMessage"
    log.info("POST Telegram sendMessage chat=%s (%d chars, buttons=%s)",
             chat_id, len(text), reply_markup is not None)
    resp = httpx.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("result")


def answer_callback(callback_query_id: str, text: Optional[str] = None) -> None:
    """Answer a callback_query so the button stops spinning (and show a toast)."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    url = f"{API_BASE}/bot{_token()}/answerCallbackQuery"
    log.info("POST Telegram answerCallbackQuery (text=%r)", text)
    resp = httpx.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()


def edit_message_text(chat_id, message_id: int, text: str,
                      reply_markup: Optional[dict] = None) -> None:
    """Edit an existing message's text (and optionally its inline keyboard)."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"{API_BASE}/bot{_token()}/editMessageText"
    log.info("POST Telegram editMessageText msg=%s (%d chars)", message_id, len(text))
    resp = httpx.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()
