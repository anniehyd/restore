# AnAn
 (安安 in Chinese means peace and rest)

[![tests](https://github.com/anniehyd/restore/actions/workflows/ci.yml/badge.svg)](https://github.com/anniehyd/restore/actions/workflows/ci.yml)

Some mornings I wake up ready for everything. Others, I feel so cooked before 9am — and I never know which one I'm getting.

I used to wake up almost every hour, and that randomness wrecks my focus and decision-making all day.
So I built AnAn: a little companion that actually knows how I slept.

When my Sleep Focus ends, an iOS Shortcut quietly ships last night's sleep data to AnAn. It cross-checks that against today's calendar and pings me on Telegram with a short, human morning brief. Rough night? It slips a 20-minute recovery block into my calendar before the day runs me over.

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
                 send Telegram     (if poor sleep)     save snapshot
                 (or WhatsApp/     write Restore       for GET / + /latest
                  ntfy)            block to calendar
```

Endpoints: `POST /wake` (the flow), `POST /telegram` (chat webhook),
`GET+POST /whatsapp` (chat webhook), `GET /latest` (last brief as JSON),
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
  whatsapp_client.py # Meta WhatsApp Cloud API send (alternate channel)
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
  `TELEGRAM_CHAT_ID`. For chat replies, register the webhook with
  `scripts/set_telegram_webhook.py` once deployed.

## Weather + AQI alerts

Silence by default: normal weather sends nothing, ever. AnAn only pings you
for actionable conditions, via [Open-Meteo](https://open-meteo.com) (no API
key):

- **7:00 AM ET morning check** (scans today 8 AM–10 PM): one combined message
  for rain ≥50% chance, any snow, feels-like ≥95°F, or feels-like ≤20°F.
- **AQI monitor** (every 2h, 8 AM–8 PM ET): strong alert at AQI ≥150, one
  softer heads-up per day at 101–149, and a one-time all-clear when a bad-air
  day recovers. Banded dedupe — no repeat alerts unless the air gets *worse*
  or it's a new day.
- The advisor also sees current conditions, so AnAn won't suggest a walk when
  it's about to pour or the air is rough.

Location and all thresholds live in `.env.example` (`WEATHER_LAT/LON`,
`RAIN_PROB_THRESHOLD`, `AQI_ALERT`, `AQI_SOFT`, `HEAT_F`, `COLD_F`); disable
entirely with `WEATHER_ALERTS_ENABLED=false`. Preview without sending:

```bash
python scripts/weather_dry_run.py
```

## Delivery channels

`PUSH_CHANNEL` selects where the brief goes: `telegram` (default), `whatsapp`,
`ntfy`, `both` (= telegram+ntfy), or a comma list like `telegram,ntfy`.
Telegram is the chat-companion experience; ntfy is kept as a demo fallback.
Each channel fails independently — a delivery hiccup never fails `/wake`.

### WhatsApp (optional alternate, Meta Cloud API)

Requires a Meta developer account at
[developers.facebook.com](https://developers.facebook.com): create a
**Business**-type app and add the **WhatsApp** product.

1. On **WhatsApp → API Setup** you get a free **test number**: copy its
   **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`, and add your own phone
   as a verified recipient. Put your number (digits only, with country code)
   in `WHATSAPP_TO`.
2. The dashboard's temporary token expires in 24h. For a permanent one:
   **Business Settings → Users → System Users** → create a system user, assign
   the app, generate a token with `whatsapp_business_messaging` — that's
   `WHATSAPP_ACCESS_TOKEN`.
3. **Webhook (chat + buttons):** on **WhatsApp → Configuration**, set the
   callback URL to `https://<your-app>.fly.dev/whatsapp`, enter the same
   string you put in `WHATSAPP_VERIFY_TOKEN`, and subscribe to the
   **messages** field. Set `WHATSAPP_APP_SECRET` (App **Settings → Basic**) so
   the server can verify Meta's signature on each delivery.
4. Smoke-test with `python scripts/check_whatsapp.py`, then set
   `PUSH_CHANNEL=whatsapp`.

**The 24-hour window rule:** WhatsApp only allows free-form bot messages
within 24h of *your* last message to it. If a morning brief hits a closed
window, AnAn sends the pre-approved template named in `WHATSAPP_TEMPLATE_NAME`
— replying reopens the window. Unlike Telegram, WhatsApp can't edit sent
messages, so booking confirmations arrive as new messages. (Telegram has none
of these limits — bot messages work any time.)

## Configuration

All secrets and config come from environment variables — see `.env.example`.
Key ones: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `ICLOUD_USERNAME` /
`ICLOUD_APP_PASSWORD`, `CALENDAR_WRITE_ENABLED`, `PUSH_CHANNEL`,
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`, and `WEBHOOK_SECRET`
(WhatsApp vars only if you opt into that channel).

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
  TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 24) \
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
