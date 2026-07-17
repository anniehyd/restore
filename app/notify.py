"""Push the morning brief to my phone via ntfy.sh.

ntfy is dead simple: POST the message body to https://ntfy.sh/<topic> and any
device subscribed to that topic gets a notification. The topic name is the only
"auth", so it must be long and unguessable (see NTFY_TOPIC in .env.example).

Reads NTFY_URL (default https://ntfy.sh) and NTFY_TOPIC from the environment.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

NTFY_BASE = os.environ.get("NTFY_URL", "https://ntfy.sh")


def send_push(title: str, message: str) -> None:
    """POST a notification to the configured ntfy topic.

    Raises RuntimeError if NTFY_TOPIC is unset, and httpx.HTTPError on a
    network/HTTP failure — callers decide whether to treat a push failure as
    fatal.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not set; cannot send push")

    url = f"{NTFY_BASE}/{topic}"
    log.info("POST ntfy %s (title=%r, %d chars)", url, title, len(message))
    resp = httpx.post(
        url,
        content=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "default",
            "Tags": "sleeping",  # renders a 😴 icon on the notification
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    log.info("ntfy push ok (status=%s)", resp.status_code)
