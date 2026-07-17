# Restore

A personal, sleep-aware morning advisor. When I wake up, it reads last night's
sleep, looks at today's calendar, and pushes a short, actionable morning brief
to my phone.

## What it does

The flow is triggered when my Sleep Focus ends (Apple Watch / iPhone). An iOS
Shortcut POSTs my sleep data to a webhook, and the backend then:

1. **Parses** last night's sleep metrics — total sleep, deep/REM/core stages,
   HRV, resting heart rate — from the [Health Auto Export](https://www.healthexportapp.com/)
   JSON format.
2. **Fetches** today's events from my Apple/iCloud Calendar (via CalDAV).
3. **Generates** a short morning brief via the Claude API (`claude-sonnet-4-6`):
   an energy assessment, one warning about today's schedule, and one concrete
   recovery suggestion tied to an actual free slot.
4. **Pushes** the brief to my phone via [ntfy.sh](https://ntfy.sh).
5. **(Stretch)** Writes a "Restore" recovery block directly into my calendar.

## Stack

- **Python 3.11**, **FastAPI**, deployed as a single service.
- **httpx** for outbound API calls (Claude, ntfy, Telegram); **caldav** for
  iCloud Calendar.
- **pydantic** for models and payload validation.
- **pytest** for tests, driven by recorded fixture payloads.
- Secrets via **environment variables only** — never hardcode keys.

This is a hackathon project. Keep it small: prefer one clear module per concern
over abstractions. Reach for a class only when state genuinely requires it.

## Structure

```
app/
  main.py            # FastAPI app + webhook endpoint (orchestrates the flow)
  sleep.py           # parse Health Auto Export payload -> sleep metrics
  calendar_client.py # iCloud Calendar read/write (CalDAV)
  advisor.py         # Claude API call + prompt construction
  notify.py          # ntfy push
tests/               # pytest, using recorded fixture payloads
.env.example         # lists every required env var
```

One module per concern. `main.py` wires them together; the concern modules
don't import each other beyond their models.

## Coding preferences

- **Type hints everywhere.** Small functions, single responsibility.
- **No classes** unless state genuinely requires it.
- **Log every external API call** — outbound request and the response status.
- **Fail loudly.** Clear error messages; I'd rather see a stack trace than a
  silent skip. Don't swallow exceptions or fall back to empty defaults on error.
- **Secrets from the environment only.** Every required var is documented in
  `.env.example`; read them once at startup and fail fast if any are missing.

## Testing

- Use **recorded fixture payloads** for the Health Auto Export webhook so the
  sleep parser can be tested without a live device.
- Mock or stub the external APIs (Claude, iCloud Calendar, ntfy, Telegram) — tests should
  not make real network calls.

## Notes for Claude Code

- Claude API model is `claude-sonnet-4-6`. Confirm the exact model id and API
  usage against the current SDK before wiring the call — don't guess params.
- When in doubt, favor readability and directness over cleverness. This is a
  small project meant to be understood at a glance.
