"""The execution engine — the single entry GitHub Actions calls.

One `tick()` drains pending events through the core and delivers replies.
Offline-testable (NEXUS_AI=stub, temp NEXUS_DB, notify=False).

Hardened after audit:
  * claim-based processing: events are atomically marked in-progress, so two
    overlapping ticks (cron + dispatch) never double-process a row.
  * per-event isolation: a poison event is retried up to MAX_ATTEMPTS then
    dead-lettered — it can no longer wedge the queue for everything behind it.
  * replies go to an outbox drained with retry, so a Telegram blip after
    mark_handled no longer silently drops the reply.
  * logging is metadata-only by default (set NEXUS_DEBUG_BODY=1 to include
    bodies) so a public Actions log never leaks stored secrets.
"""
import os
import time

from .core import Nexus, safe
from .memory import Memory
from . import telegram as tg


def _log_body():
    return os.environ.get("NEXUS_DEBUG_BODY") == "1"


def tick(db=None, brain=None, notify=True, log=print):
    """Process all pending events (claimed). Returns a summary dict."""
    mem = Memory(db)
    nexus = Nexus(mem, brain=brain)
    processed, failed, replies = 0, 0, []

    try:
        for event_id, source, sender, text in mem.claim():
            try:
                res = nexus.handle_event(event_id, source, sender, text)
            except Exception as e:                    # isolate, don't abort
                mem.bump_attempt(event_id)
                failed += 1
                if log:
                    # safe() strips control chars, _redact blanks tokens — an
                    # exception can echo a provider URL or forge log lines.
                    log("event %s failed: %s" % (event_id, tg._redact(safe(e))))
                continue
            processed += 1
            replies.append(res)
            if log:
                line = "[%s] intent=%s action=%s" % (
                    source, res["intent"], res["action"])
                if _log_body():
                    line += " -> " + res["reply"][:80]
                log(line)
            if notify and source == "telegram" and res["reply"]:
                mem.outbox_add(sender, res["reply"])

        if notify:
            _drain_outbox(mem, log)
    finally:
        mem.close()

    if log:
        log("tick done: %d processed, %d failed" % (processed, failed))
    return {"processed": processed, "failed": failed, "at": time.time(),
            "replies": replies}


def _drain_outbox(mem, log=print):
    """Deliver queued replies with retry/backoff via the Telegram gateway."""
    for out_id, chat_id, text, attempts in mem.outbox_pending():
        r = tg.send(text, chat_id=chat_id)
        mem.outbox_mark(out_id, bool(r.get("ok")))
        if log and not r.get("ok"):
            log("  (send failed, attempt %d: %s)" % (attempts + 1,
                                                     r.get("err", "?")))


def ingest(text, source="cli", sender=None, db=None, raw=None, raw_id=None):
    """Push a signal into the queue (used by CLI, webhook, and tests)."""
    mem = Memory(db)
    try:
        return mem.add_event(source, text, sender=sender, raw=raw, raw_id=raw_id)
    finally:
        mem.close()


def doctor(db=None):
    """Environment self-check — reports readiness without side effects."""
    from . import ai as ai_mod
    checks = [
        ("db", db or os.environ.get("NEXUS_DB", "nexus-zero.db")),
        ("brain", ai_mod.get_brain().name),
        ("telegram_token", "set" if tg.TOKEN else "MISSING (bot won't send)"),
        ("telegram_chat", tg.CHAT_ID or "MISSING"),
        ("chat_allowlist", "on" if tg.CHAT_ID else "OFF (any chat accepted)"),
        ("gateway_secret", "set" if len(os.environ.get("NEXUS_GATEWAY_SECRET", "")) >= 24
         else "MISSING/weak (<24 chars)"),
        ("hf_token", "set" if os.environ.get("HF_TOKEN") else "unset (hf brain idle)"),
    ]
    ok = True
    out = ["Nexus-Zero doctor", "=" * 44]
    for k, v in checks:
        out.append("  %-16s %s" % (k, v))
        if "MISSING" in str(v) or "OFF" in str(v):
            ok = False
    out.append("=" * 44)
    out.append("ready: %s" % ("YES" if ok else "partial — set MISSING secrets"))
    return "\n".join(out), ok
