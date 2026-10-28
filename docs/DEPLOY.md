# Nexus-ZeroBaran — Deployment Guide

From zero to a live, Telegram-driven sovereign agent — **using only free tiers**. Final cost: **$0**.

## Architecture

```text
Telegram → Cloudflare Worker (verify secret, wake via repository_dispatch)
  → GitHub Actions (nexus.yml) → core tick → Telegram reply
```

## Step 1 — Telegram bot

1. Open **@BotFather** in Telegram → `/newbot` → copy the **token**.
2. Get your chat id (needed both to reply and as the inbound **allowlist**):

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates"
```
Copy the numeric `"chat":{"id":...}`.

## Step 2 — Local memory & brain (offline)

```bash
export NEXUS_DB=nexus-zero.db
export NEXUS_AI=stub        # stub | ollama | hf
python3 -m nexus_zero.cli init
python3 -m nexus_zero.cli remember language arabic
python3 -m nexus_zero.cli tick
python3 -m nexus_zero.cli recall
python3 -m nexus_zero.cli doctor     # honest readiness check
```

- `NEXUS_AI=ollama` → install Ollama, `ollama pull qwen2.5-coder:7b`, endpoint via `OLLAMA_URL` (default `http://localhost:11434`).
- `NEXUS_AI=hf` → set `HF_TOKEN` (Hugging Face serverless router).

## Step 3 — Cloudflare Worker (gateway)

```bash
npm i -g wrangler && wrangler login
wrangler secret put NEXUS_GATEWAY_SECRET      # >= 24 chars, random
wrangler secret put TELEGRAM_WEBHOOK_SECRET   # >= 24 chars, random
wrangler secret put GH_OWNER
wrangler secret put GH_REPO
wrangler secret put GH_DISPATCH_TOKEN         # a fine-grained PAT / classic token with repo dispatch
wrangler deploy
```

Then point Telegram at the Worker, passing the **same** `TELEGRAM_WEBHOOK_SECRET` as Telegram's `secret_token` (Telegram sends it back as the `X-Telegram-Bot-Api-Secret-Token` header, which the Worker verifies):

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://<your-worker>.workers.dev/telegram",
       "secret_token":"<TELEGRAM_WEBHOOK_SECRET>"}'
```

## Step 4 — GitHub repo + Actions

```bash
gh repo create <repo-name> --private
git remote add origin git@github.com:<GH_OWNER>/<repo-name>.git
git push -u origin main
```

Add **Settings → Secrets and variables → Actions**:

| Secret | Required | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | bot token |
| `TELEGRAM_CHAT_ID` | ✅ | your chat id — **also the inbound allowlist**; updates from other chats are dropped |
| `NEXUS_GATEWAY_SECRET` | ✅ | must match the Worker's, **>= 24 chars** |
| `NEXUS_AI` | optional | `stub` \| `ollama` \| `hf` (default `stub`) |
| `HF_TOKEN` | optional | only if `NEXUS_AI=hf` |

The workflow (`.github/workflows/nexus.yml`) runs the hourly cron `17 * * * *` plus `workflow_dispatch` / `repository_dispatch`. It serializes ticks (`concurrency`), injects secrets via `env:` mapping (never shell interpolation), runs `doctor`, drains the queue, and commits the SQLite memory back with `git add -f` (the file is `.gitignore`d, so a plain `add` silently skips it).

## Cost = $0

| Component | Service | Plan | Cost |
|---|---|---|---|
| Gateway | Cloudflare Workers | Free (~100k req/day) | $0 |
| Engine | GitHub Actions | Free (~2,000 min/mo private) | $0 |
| Memory | SQLite (file in repo) | Free | $0 |
| Brain | Ollama / HF serverless / Stub | Free | $0 |
| Interface | Telegram Bot API | Free | $0 |

Hourly cron ≈ 720 runs ≈ ~15–20 min/mo — comfortably inside the free quota.

## Troubleshooting

- **Webhook 403** → secret mismatch. The Worker checks the Telegram `secret_token` header (`TELEGRAM_WEBHOOK_SECRET`) on `/telegram` and `X-Nexus-Secret` (`NEXUS_GATEWAY_SECRET`) on `/ingest`. Keep each pair identical at both ends.
- **Bot doesn't reply** → run `doctor`; confirm `TELEGRAM_CHAT_ID` matches the chat you're messaging (the allowlist drops strangers); check the Actions logs; confirm the event reached the queue.
- **Memory not persisting** → the commit step needs `git add -f` (it's there) and a **private** repo if the DB contains secrets.
- **Remote DSN refused** → `NEXUS_DB=libsql://…` / `https://…` is **rejected by design**: `sqlite3.connect` cannot open a URL, and v1.0.0's "swap to Turso" claim was false. Use the local file (persisted by Actions) or bring your own libsql HTTP adapter behind `Memory`.

## License & Owner

MIT © 2026 Baran Avisat — Aodı Akdıro Suite · baranavisat@gmail.com · WhatsApp +90 531 326 37 22
