"""The companion's identity, in one place.

Set the name, emoji palette, and how playful-vs-gentle it is (1-5) via env; both
the morning brief and the chat replies read from here so the voice stays
consistent. Read at call time so changing the env + restart is all it takes.
"""

from __future__ import annotations

import os

_VIBES = {
    1: "very calm and gentle; soothing; barely any emoji.",
    2: "calm and warm; the occasional emoji.",
    3: "warm and lightly playful; a couple of emoji.",
    4: "upbeat and playful; emoji welcome.",
    5: "bubbly, cute, and very playful; emoji throughout.",
}


def name() -> str:
    # 安安 (ānān) — "rest and peace". Override with COMPANION_NAME.
    return os.environ.get("COMPANION_NAME", "anan")


def emoji() -> str:
    return os.environ.get("COMPANION_EMOJI", "🌿🌙💛")


def playfulness() -> int:
    try:
        p = int(os.environ.get("COMPANION_PLAYFULNESS", "3"))
    except ValueError:
        p = 3
    return max(1, min(5, p))


def telegram_url() -> str:
    """t.me link to the bot, or '' if the username isn't configured."""
    user = os.environ.get("COMPANION_TELEGRAM_USERNAME", "ananrestbot").lstrip("@")
    return f"https://t.me/{user}" if user else ""


def prompt() -> str:
    """The persona line injected into both system prompts."""
    p = playfulness()
    return (
        f"Your name is {name()}. Vibe (playfulness {p}/5): {_VIBES[p]} "
        f"When you use emoji, favor this palette: {emoji()}."
    )
