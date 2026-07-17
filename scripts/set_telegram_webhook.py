"""Register (or clear) the Telegram webhook for the deployed app.

Points Telegram at https://<your-host>/telegram and sets a secret_token that the
server checks on every call (X-Telegram-Bot-Api-Secret-Token). The SAME secret
must be set as TELEGRAM_WEBHOOK_SECRET on the server, so this script reads it from
the environment rather than inventing one.

Usage:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... \\
        python scripts/set_telegram_webhook.py https://restore-annie.fly.dev

    # remove the webhook (e.g. to go back to local polling):
    python scripts/set_telegram_webhook.py --delete
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

API_BASE = "https://api.telegram.org"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", help="public base URL, e.g. https://restore-annie.fly.dev")
    ap.add_argument("--delete", action="store_true", help="remove the webhook")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: set TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1

    if args.delete:
        resp = httpx.post(f"{API_BASE}/bot{token}/deleteWebhook", timeout=10.0)
        print(resp.status_code, resp.text)
        return 0 if resp.status_code == 200 else 1

    if not args.host:
        print("ERROR: pass the public host URL (or --delete)", file=sys.stderr)
        return 1
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        print("ERROR: set TELEGRAM_WEBHOOK_SECRET (must match the server's env)", file=sys.stderr)
        return 1

    url = f"{args.host.rstrip('/')}/telegram"
    resp = httpx.post(
        f"{API_BASE}/bot{token}/setWebhook",
        json={
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],  # buttons too
            "drop_pending_updates": True,
        },
        timeout=10.0,
    )
    print("setWebhook:", resp.status_code, resp.text)
    if resp.status_code != 200:
        return 1

    # Register the command menu (BotFather-style descriptions, done via the API).
    cmds = httpx.post(
        f"{API_BASE}/bot{token}/setMyCommands",
        json={"commands": [
            {"command": "today", "description": "today's events + open slots"},
            {"command": "sleep", "description": "last night's sleep numbers"},
            {"command": "reset", "description": "(demo) re-prime the bad-night demo"},
        ]},
        timeout=10.0,
    )
    print("setMyCommands:", cmds.status_code, cmds.text)
    print(f"\nWebhook set to {url}. Make sure the server has the same "
          f"TELEGRAM_WEBHOOK_SECRET set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
