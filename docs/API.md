# API.md — Nexus-ZeroBaran

Three-line quickstarts. Every command is offline-safe; network is used only
by the brain/telegram you explicitly configure (`NEXUS_AI`, `TELEGRAM_TOKEN`).

## CLI (stdlib-only)

```bash
export NEXUS_AI=stub                 # offline brain (or: ollama | hf)
python3 -m nexus_zero.cli init
python3 -m nexus_zero.cli ingest "remember language: arabic"
python3 -m nexus_zero.cli tick       # the cron job: drain + reply
python3 -m nexus_zero.cli recall     # search memory / list facts
```

| Command | Purpose |
|---|---|
| `init` | create the memory store (`NEXUS_DB` or `nexus-zero.db`) |
| `ingest TEXT [--source s] [--sender u]` | queue a signal |
| `tick [--no-notify] [--json]` | process the queue (Actions calls this) |
| `remember KEY VALUE` / `recall [QUERY]` | long-term facts + full-text search |
| `doctor` | environment self-check (exit 1 if secrets missing) |
| `serve [--host --port]` | local secret-guarded webhook receiver |

## Python library (3 lines)

```python
from nexus_zero.memory import Memory
from nexus_zero.core import Nexus
from nexus_zero import ai

n = Nexus(Memory("nexus-zero.db"), brain=ai.get_brain("stub"))
res = n.handle_event(None, "cli", "u1", "remember city: istanbul")
print(res["action"], res["reply"])   # store-fact Remembered: city = istanbul
```

### `Nexus.handle_event(event_id, source, sender, text)` → dict
`{intent, confidence, action, reply, thread}` — classify → recall → route →
draft → log, all in one auditable call. `event_id=None` skips the
mark-handled step.

### `workflow` (the runner Actions invokes)
```python
from nexus_zero import workflow
workflow.ingest("give me a report", source="telegram", sender="777")  # enqueue
summary = workflow.tick(db="nexus-zero.db", notify=False)             # process
print(summary["processed"])                                           # drained count
```

## Memory (`nexus_zero.memory.Memory`)
- `add_event / pending_events / mark_handled` — append-only ingest queue
- `add_message / history / search` — conversation + FTS5 recall
- `set_fact / get_fact` — long-term preferences (upsert)
- `log_run` — audit trail of every decision the core made

Point `NEXUS_DB` at a local SQLite file (the default). A remote libsql/Turso
URL is rejected by design — `sqlite3.connect` cannot open a URL, so the old
"swap to Turso" claim was false and is now refused loudly. To go remote, bring
your own HTTP libsql adapter behind the `Memory` interface.

## Brains (`nexus_zero.ai.get_brain(kind)`)
| kind | Backend | Needs |
|---|---|---|
| `stub` | deterministic rules, zero network | nothing (tests/demo) |
| `ollama` | local models via `OLLAMA_URL` | a running Ollama |
| `hf` | Hugging Face serverless router | `HF_TOKEN` |

All three implement `classify(text)`, `draft(text, context)`, `embed(text)`.
A rule-based intent floor (remember/recall/schedule/report/task/chat) backs
every backend, so a weak or offline model never breaks classification.

---

MIT License © 2026 Baran Avisat — Aodı Akdıro Suite
Support: baranavisat@gmail.com · WhatsApp +90 531 326 37 22
