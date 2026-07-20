# Rise

[![tests](https://github.com/anniehyd/restore/actions/workflows/ci.yml/badge.svg)](https://github.com/anniehyd/restore/actions/workflows/ci.yml)

Some mornings I wake up ready for everything. Others, I feel so cooked before 9am — and I never know which one I'm getting.

I used to wake up almost every hour, and that randomness quietly wrecks my focus and decision-making all day.
So I built Rise: a little companion that actually knows how I slept.

When my Sleep Focus ends, an iOS Shortcut quietly ships last night's sleep data to Rise. It cross-checks that against today's calendar and pings me on Telegram with a short, human morning brief. Rough night? It slips a 20-minute recovery block into my calendar before the day runs me over.

## The brief

Each morning Restore generates:

- an **energy assessment** based on sleep quality (stages, HRV, resting HR),
- **one flag** about today's schedule, and
- **one recovery suggestion** tied to an actual free slot in my calendar.

## How it works

```
Health Auto Export ──POST sleep JSON──▶ POST /wake (FastAPI, bearer-auth)
                                          │
                    ┌─────────────────────┼───────────────────────┐
                    ▼                     ▼                        ▼
             parse sleep          read calendar             Claude API
           (Health Auto          (iCloud CalDAV)           (brief + restore
             Export JSON)                                    block proposal)
                    │                     │                        │
                    └─────────────────────┴────────────────────────┘
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                 send Telegram     (if poor sleep)     save snapshot
                 (or ntfy/both)    write Restore       for GET / + /latest
                                   block to calendar
```

Endpoints: `POST /wake` (the flow), `GET /latest` (last brief as JSON),
`GET /` (a single-file demo page), `GET /health`.

## Stack

Python 3.11 · FastAPI · httpx · pydantic · pytest. Deployed to Fly.io as a
single Docker service.

## Project layout

```
app/
  main.py            # FastAPI app + /wake, /latest, / , /health
  sleep.py           # parse Health Auto Export payload -> SleepSummary
  calendar_client.py # iCloud Calendar read + write via CalDAV (Restore block)
  advisor.py         # Claude API call + prompt (structured JSON)
  notify.py          # ntfy push (fallback channel)
  telegram_client.py # Telegram Bot API sendMessage (primary channel)
  store.py           # last-brief snapshot (JSON file)
  demo_page.py       # the GET / demo page (self-contained HTML)
scripts/
  dry_run.py         # iterate on the advisor prompt against a fixture
  demo_reset.py      # prime the demo page with a known bad-night state
tests/               # pytest with recorded fixture payloads
Dockerfile · fly.toml · .env.example
```

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then fill in the values
uvicorn app.main:app --reload
```

- **iCloud Calendar:** generate an app-specific password at appleid.apple.com
  (Sign-In & Security → App-Specific Passwords) and set `ICLOUD_USERNAME` +
  `ICLOUD_APP_PASSWORD`. Your calendar must live in iCloud. No OAuth, no token
  files — the same two vars work locally and in deployment.
- **Telegram:** create a bot with **@BotFather** (`/newbot`), grab the token,
  send your bot a message, then read your `chat.id` from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`. Set `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`.

## Delivery channels

`PUSH_CHANNEL` selects where the brief goes: `telegram` (default), `ntfy`, or
`both`. Telegram is the chat-companion experience; ntfy is kept as a demo
fallback. Each channel fails independently — a delivery hiccup never fails
`/wake`.

## Configuration

All secrets and config come from environment variables — see `.env.example`.
Key ones: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `ICLOUD_USERNAME` /
`ICLOUD_APP_PASSWORD`, `CALENDAR_WRITE_ENABLED`, `PUSH_CHANNEL`,
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`, and `WEBHOOK_SECRET`.

## Deploy (Fly.io)

```bash
fly apps create restore-<unique>          # match app= in fly.toml
WEBHOOK_SECRET=$(openssl rand -hex 24); echo "SAVE THIS: $WEBHOOK_SECRET"
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  CLAUDE_MODEL=claude-sonnet-4-6 \
  PUSH_CHANNEL=telegram \
  TELEGRAM_BOT_TOKEN=8123456789:AA... \
  TELEGRAM_CHAT_ID=123456789 \
  NTFY_TOPIC=restore-annie-x7k2p9 \
  WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  ICLOUD_USERNAME=you@icloud.com ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop \
  CALENDAR_WRITE_ENABLED=false
fly deploy
```

`POST /wake` requires `Authorization: Bearer <WEBHOOK_SECRET>`.

## Testing

```bash
pytest
```

Tests run against recorded fixture payloads; external services (Claude, iCloud,
Telegram, ntfy) are stubbed — no live device or network calls.

End-to-end smoke test against a running server:

```bash
curl -sX POST http://127.0.0.1:8000/wake \
  -H "Authorization: Bearer $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  --data @tests/fixtures/sleep_bad_night.json | python3 -m json.tool
```
