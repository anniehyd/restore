# AnAn
 (安安 in Chinese means peace and rest)

[![tests](https://github.com/anniehyd/restore/actions/workflows/ci.yml/badge.svg)](https://github.com/anniehyd/restore/actions/workflows/ci.yml)

Some mornings I wake up ready for everything. Others, I feel so cooked before 9am — and I never know which one I'm getting.

I used to wake up almost every hour, and that randomness wrecks my focus and decision-making all day.
So I built AnAn: a little companion that actually knows how I slept.

When my Sleep Focus ends, an iOS Shortcut quietly ships last night's sleep data to AnAn. It cross-checks that against today's calendar and pings me on WhatsApp with a short, human morning brief. Rough night? It slips a 20-minute recovery block into my calendar before the day runs me over.

## The brief

Each morning AnAn generates:

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
                 send WhatsApp     (if poor sleep)     save snapshot
                 (or Telegram/     write Restore       for GET / + /latest
                  ntfy)            block to calendar
```

Endpoints: `POST /wake` (the flow), `GET+POST /whatsapp` (chat webhook),
`POST /telegram` (chat webhook), `GET /latest` (last brief as JSON),
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
  whatsapp_client.py # Meta WhatsApp Cloud API send (primary channel)
  telegram_client.py # Telegram Bot API sendMessage (alternate channel)
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
- **WhatsApp:** see the walkthrough below.

## WhatsApp setup (Meta Cloud API)

1. At [developers.facebook.com](https://developers.facebook.com) create an app
   (type **Business**) and add the **WhatsApp** product. Meta walks you through
   creating a business portfolio if you don't have one.
2. On **WhatsApp → API Setup** you get a free **test number**: copy its
   **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`, and add your own phone
   as a recipient (test numbers can message up to 5 verified numbers — plenty
   for a personal bot). Put your number (digits only, with country code) in
   `WHATSAPP_TO`.
3. The dashboard's temporary token expires in 24h. For a permanent one:
   **Business Settings → Users → System Users** → create a system user, assign
   the app, generate a token with `whatsapp_business_messaging` — that's
   `WHATSAPP_ACCESS_TOKEN`.
4. **Webhook (chat + buttons):** on **WhatsApp → Configuration**, set the
   callback URL to `https://<your-app>.fly.dev/whatsapp`, enter the same
   string you put in `WHATSAPP_VERIFY_TOKEN`, and subscribe to the
   **messages** field. Set `WHATSAPP_APP_SECRET` (App **Settings → Basic**) so
   the server can verify Meta's signature on each delivery.
5. Send the bot any WhatsApp message from your phone, then
   `python scripts/check_whatsapp.py` to confirm the reverse direction works.

**The 24-hour window rule:** WhatsApp only allows free-form bot messages
within 24h of *your* last message to it. Chat with AnAn daily and you'll never
notice. If a morning brief does hit a closed window, AnAn sends the
pre-approved template named in `WHATSAPP_TEMPLATE_NAME` (create one under
**WhatsApp → Message templates**, e.g. "good morning 🌿 message me and I'll
share today's brief") — replying reopens the window. Unlike Telegram, WhatsApp
can't edit sent messages, so booking confirmations arrive as new messages.

## Delivery channels

`PUSH_CHANNEL` selects where the brief goes: `whatsapp`, `telegram`, `ntfy`,
`both` (= telegram+ntfy), or a comma list like `whatsapp,ntfy`. WhatsApp is
the chat-companion experience; ntfy is kept as a demo fallback. Each channel
fails independently — a delivery hiccup never fails `/wake`.

Telegram remains fully supported: create a bot with **@BotFather** (`/newbot`),
set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, register the webhook with
`scripts/set_telegram_webhook.py`, and set `PUSH_CHANNEL=telegram`.

## Configuration

All secrets and config come from environment variables — see `.env.example`.
Key ones: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `ICLOUD_USERNAME` /
`ICLOUD_APP_PASSWORD`, `CALENDAR_WRITE_ENABLED`, `PUSH_CHANNEL`,
`WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_TO` /
`WHATSAPP_VERIFY_TOKEN` / `WHATSAPP_APP_SECRET`, `NTFY_TOPIC`, and
`WEBHOOK_SECRET`.

## Deploy (Fly.io)

```bash
fly apps create restore-<unique>          # match app= in fly.toml
WEBHOOK_SECRET=$(openssl rand -hex 24); echo "SAVE THIS: $WEBHOOK_SECRET"
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  CLAUDE_MODEL=claude-sonnet-4-6 \
  PUSH_CHANNEL=whatsapp \
  WHATSAPP_ACCESS_TOKEN=EAAG... \
  WHATSAPP_PHONE_NUMBER_ID=123456789012345 \
  WHATSAPP_TO=15551234567 \
  WHATSAPP_VERIFY_TOKEN=$(openssl rand -hex 24) \
  WHATSAPP_APP_SECRET=your-meta-app-secret \
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
WhatsApp, Telegram, ntfy) are stubbed — no live device or network calls.

End-to-end smoke test against a running server:

```bash
curl -sX POST http://127.0.0.1:8000/wake \
  -H "Authorization: Bearer $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  --data @tests/fixtures/sleep_bad_night.json | python3 -m json.tool
```
