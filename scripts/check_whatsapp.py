"""Smoke-test the WhatsApp Cloud API credentials: send yourself one message.

Usage:
    python scripts/check_whatsapp.py            # sends a test message
    python scripts/check_whatsapp.py "hi 🌿"    # custom text

Reads WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TO from the
environment (source your .env first). Remember: free-form sends only work
within 24h of your last message to the bot — message it once from your phone
before running this.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.whatsapp_client import OutsideWindowError, send_message  # noqa: E402


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "test from AnAn 🌿 (check_whatsapp.py)"
    try:
        result = send_message(text)
    except OutsideWindowError:
        print("✗ 24h window is closed — send the bot a WhatsApp message from "
              "your phone first, then rerun.")
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI feedback
        print(f"✗ send failed: {exc}")
        return 1
    wamid = (result.get("messages") or [{}])[0].get("id", "?")
    print(f"✓ sent ({wamid}) — check your WhatsApp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
