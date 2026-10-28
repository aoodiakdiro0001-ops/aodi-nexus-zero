# SECURITY.md — Nexus-ZeroBaran

## Threat model (what this defends, and what it does not)

Nexus-Zero runs on **your** free-tier accounts (GitHub, Telegram, Cloudflare,
optionally Turso/Hugging Face) and holds **your** tokens in **your** secrets
store. It is a personal automation mesh, not a multi-tenant service. The
design assumes:

- **The operator is trusted.** Anyone with your GitHub/Telegram/CF tokens can
  already do anything this tool can. We do not defend against a compromised
  operator account.
- **The public webhook is untrusted.** The Cloudflare Worker and the local
  `serve` receiver both require a shared secret (`X-Nexus-Secret`), compared
  in constant time and **failing closed** on empty/unset. An attacker who
  finds your Worker URL cannot enqueue work without the secret.
- **Inference backends see only what you send them.** The offline stub sends
  nothing anywhere. Ollama stays on your machine. The Hugging Face backend
  sends only the messages you route to it — pick it only if you accept that
  provider seeing that content. This is stated plainly, not hidden.

## What we deliberately do NOT do

- **No phone-home, no telemetry, no analytics.** The only network calls are
  to endpoints you explicitly configured (your bot, your chosen inference
  provider, your Worker).
- **No secrets in code.** Tokens come from environment variables / GitHub
  Secrets / `wrangler secret` only. The repo ships with none.
- **No hidden account access.** The GitHub Actions job uses `contents: write`
  solely to commit the updated memory file back to your own repo; it requests
  no other scope.

## Data flow (sovereignty in one glance)

```
Telegram/you → Cloudflare Worker (edge, stateless, secret-guarded)
             → GitHub repository_dispatch → Actions runner (your repo)
             → nexus_zero core (classify/recall/act) → memory (your DB)
             → reply back to Telegram
```

Your message passes through the edge only to reach your own runner. Memory
lives where you point `NEXUS_DB` — a local file, or your own Turso/Supabase.
You can delete everything by deleting the repo and the memory database.

## Responsible use

This tool automates **accounts you own** using **tokens you control**. It
respects each service's terms. Do not point it at systems or accounts you are
not authorized to operate. The hourly Actions cron is a convenience schedule,
not a bypass of any rate limit — keep your usage within each free tier's
published quotas.

## Reporting an issue

Email **baranavisat@gmail.com** with the subject `[Nexus-Zero security]`.
We would rather hear about a weakness from you than from the internet.

---

MIT License © 2026 Baran Avisat — Aodı Akdıro Suite
Support: baranavisat@gmail.com · WhatsApp +90 531 326 37 22
