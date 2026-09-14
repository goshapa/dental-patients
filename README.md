# Dental Patient Records

A minimal patient-records system for a dental practice: a Telegram bot for
data entry and a web app for reviewing patient history — deployed serverless
on Vercel.

## How it works

- **Telegram bot** ([bot_logic.py](bot_logic.py)) — the dentist adds patients
  and visits (complaints, diagnosis, treatment, materials, x-rays/photos)
  through a step-by-step chat conversation. Runs as a stateless webhook
  (`POST /api/telegram`); conversation progress is stored per-chat in
  Postgres rather than in memory, since serverless functions don't persist
  state between requests.
- **Web app** ([app.py](app.py), Flask) — full patient list/search for the
  dentist, plus a private, unguessable link per patient
  (`/card/<token>`) that the bot hands out for quick access to one record.
- **Storage** — Postgres ([Neon](https://neon.tech), via the Vercel
  Marketplace) for records, [Vercel Blob](https://vercel.com/docs/vercel-blob)
  (private access) for x-rays/photos.

## Stack

Python, Flask, psycopg3, `requests` (raw Telegram Bot API calls — no bot
framework, since polling-based libraries don't fit a request-driven
serverless runtime), Vercel Functions (Python runtime, zero-config).

## Local development

```bash
pip install -r requirements.txt
vercel env pull          # pulls DB/Blob credentials into .env.local
vercel dev
```

The bot requires a public HTTPS URL for Telegram's webhook, so bot flows are
easiest to test against a deployed preview rather than `vercel dev`.

## Deploy

```bash
vercel link
vercel install neon            # provisions Postgres
vercel blob create-store <name> --access private
vercel env add TELEGRAM_BOT_TOKEN production
vercel env add ALLOWED_TELEGRAM_ID production   # comma-separated Telegram user IDs
vercel env add BASE_URL production              # e.g. https://your-project.vercel.app
vercel deploy --prod
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-project.vercel.app/api/telegram"
```
