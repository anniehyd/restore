"""Chunked delivery: briefs go out as one message per bubble."""

import app.main as main


def test_split_brief_on_blank_lines():
    text = "morning 🌙 happy tuesday\n\nlast night was soft, about five hours\n\nno rush today 💛"
    assert main._split_brief(text) == [
        "morning 🌙 happy tuesday",
        "last night was soft, about five hours",
        "no rush today 💛",
    ]


def test_split_brief_single_paragraph_unchanged():
    assert main._split_brief("just one bubble") == ["just one bubble"]


def test_telegram_chunks_buttons_on_last(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_telegram",
                        lambda text, markup=None: sent.append((text, markup)))
    main._tg_send_chunked("hello\n\nworld", {"inline_keyboard": []})
    assert sent == [("hello", None), ("world", {"inline_keyboard": []})]


def test_whatsapp_chunks_buttons_on_last(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_whatsapp",
                        lambda text, buttons=None: sent.append((text, buttons)))
    buttons = [{"id": "book", "title": "🌿 book 9:00 AM"}]
    main._wa_send_chunked("a\n\nb\n\nc", buttons)
    assert sent == [("a", None), ("b", None), ("c", buttons)]
