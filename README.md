# Nexus-ZeroBaran — Sovereign Agentic Mesh

**A sovereign, decentralized agentic mesh — your autonomous assistant at $0/month, with your data kept yours.**

Nexus-Zero is a small, auditable assistant that runs **100% on free tiers**: a Telegram front door, a Cloudflare Worker gateway, a GitHub Actions execution engine, SQLite memory, and a swappable brain (Ollama / Hugging Face / offline stub).

✅ **CI is offline & green** — all 27 tests in `tests/test_nexus.py` run on the Python standard library alone, with **zero network access**.

---

## نظرة سريعة — النسخة العربية

**Nexus-ZeroBaran** شبكة وكيلية حكيمة ("agentic mesh"): مساعد مستقل يعمل **كلياً على الخدمات المجانية** (صفر دولار شهرياً) ولا يُرسل بياناتك إلى خوادم الشركات الكبرى — **بياناتك تبقى ملكك**.

### كيف يعمل؟

```text
تليجرام (Bot API مجاني)
      │
      ▼
Cloudflare Worker ──► إيقاظ GitHub Actions (repository_dispatch)
      │                        │
      └───────────┬────────────┘
                  ▼
      النواة: ingest → classify → recall → route → draft → log
                  │
                  ▼
            ردّ تليجرام (مجاني)
```

### حقيقة مُعلنة (لا مبالغة)

- الذاكرة المحلية **SQLite على القرص** — Turso/libsql البعيد **يُرفض الآن برسالة صريحة** (لا ادّعاء زائف للتبديل السحابي).
- دماغ `stub` **إحلالي حتمي بلا استدلال حقيقي** — للتشغيل دون اتصال والاختبارات فقط.

---

## Quickstart — offline in seconds

Python 3.8+، مكتبة قياسية فقط. يعمل دون اتصال تماماً مع `NEXUS_AI=stub`.

```bash
export NEXUS_AI=stub
export NEXUS_DB=nexus-zero.db
python3 -m nexus_zero.cli init
python3 -m nexus_zero.cli ingest "remember language: arabic"
python3 -m nexus_zero.cli tick
python3 -m nexus_zero.cli recall
python3 -m nexus_zero.cli doctor   # honest self-check; exit 1 if not ready
```

## The Free Stack

| Component | Free service | Role |
|---|---|---|
| Gateway | Cloudflare Workers (~100k req/day) | verifies the right secret per route, wakes Actions via `repository_dispatch` |
| Engine | GitHub Actions (~2,000 min/mo private) | hourly cron `17 * * * *` + on-demand dispatch; drains the queue |
| Memory | SQLite (local file) | events (+dedupe), messages (+FTS5), facts, runs, outbox |
| Brain | Ollama (local) / Hugging Face (free) / Stub | switch via `NEXUS_AI=stub`\|`ollama`\|`hf` |
| Interface | Telegram Bot API | commands in, replies out |

## Security posture (audit-hardened)

- **Chat allowlist** — inbound Telegram updates are dropped unless `TELEGRAM_CHAT_ID` matches; a stranger who finds your bot cannot read memory or trigger work.
- **Secrets >= 24 chars**, compared in constant time; weak/empty secrets fail closed (the gateway and `serve` refuse them loudly).
- **No cross-chat leakage** — facts, search, and history are thread-scoped.
- **Claim-based ticks** — overlapping cron/dispatch runs never double-process an event.
- **Poison events** are retried then dead-lettered; a bad row can't wedge the queue.
- **Logging is metadata-only** (set `NEXUS_DEBUG_BODY=1` to include bodies) so a public Actions log never leaks stored secrets.
- See **[SECURITY.md](SECURITY.md)** for the full disclosure policy and the three project-wide commitments (no TLS/JA3 spoofing, no identity rotation, no "99.9% success" claims).

## Honest limits

- **Cron is ~hourly, not real-time.** Near-real-time comes from the Worker → dispatch → Actions path; the cron is the fallback sweep.
- **Free tiers have quotas that can change** (Cloudflare, GitHub, HF, Telegram). Budget accordingly.
- **The local store is not a multi-writer DB.** The Actions workflow serializes ticks and commits the SQLite file back (keep the repo **private** if you persist memory there).

## Deployment

Zero-to-live guide: **[docs/DEPLOY.md](docs/DEPLOY.md)** · API reference: **[docs/API.md](docs/API.md)**.

## License & Owner

MIT License © 2026 Baran Avisat — Aodı Akdıro Suite · Support: baranavisat@gmail.com · WhatsApp +90 531 326 37 22 · github.com/aoodiakdiro0001-ops
