"""The agentic core: ingest -> classify -> recall -> act -> draft -> log.

Deliberately small and auditable. Every decision is a pure function of
(message, memory, brain) — no hidden state, no phone-home. Hardened after
audit: facts/search/history are THREAD-scoped so one chat can never read
another's memory; logging is metadata-only (bodies only when NEXUS_DEBUG_BODY=1)
so a public Actions log never leaks stored secrets.
"""
import os
import re

from . import ai as ai_mod

# strip control chars (ANSI/OSC escapes) at PRINT time only — never at rest
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(s):
    return _CTRL.sub("", str(s))


def _debug_bodies():
    return os.environ.get("NEXUS_DEBUG_BODY") == "1"


class Nexus(object):
    def __init__(self, memory, brain=None):
        self.mem = memory
        self.brain = brain or ai_mod.get_brain()

    # --------------------------------------------------------- processing
    def handle_event(self, event_id, source, sender, text):
        """Process one signal end-to-end; returns a reply dict.

        The caller owns the surrounding transaction/attempt policy (see
        workflow.tick); this method itself is side-effecting on memory.
        """
        thread = sender or "default"
        text = str(text)
        user_msg_id = self.mem.add_message(thread, "user", text, event_id=event_id)

        c = self.brain.classify(text)
        intent, conf = c["intent"], c["confidence"]

        context = self.mem.history(thread, limit=8)
        # exclude the row just written so a `recall` can't match its own question
        action, reply = self._route(intent, text, thread, context,
                                    exclude_id=user_msg_id)

        self.mem.add_message(thread, "assistant", reply, event_id=event_id)
        self.mem.log_run(intent, action, text)
        if event_id is not None:
            self.mem.mark_handled(event_id)
        return {"intent": intent, "confidence": conf, "action": action,
                "reply": reply, "thread": thread}

    def _route(self, intent, text, thread, context, exclude_id=None):
        if intent == "remember":
            key, val = self._parse_kv(text)
            if key:
                self.mem.set_fact(key, val, thread=thread)
                return "store-fact", "Remembered: %s = %s" % (safe(key), safe(val))
            return "store-fact-failed", (
                "I could not find a 'key: value' pair to remember. "
                "Try: remember language: arabic")
        if intent == "recall":
            hits = self.mem.search(text, limit=5, thread=thread,
                                   exclude_id=exclude_id)
            if hits:
                body = "\n".join("- %s" % safe(h["text"]) for h in hits)
                return "recall", "Here is what I recall:\n%s" % body
            facts = self.mem.list_facts(thread=thread, limit=5)
            if facts:
                body = "\n".join("- %s: %s" % (safe(f["key"]), safe(f["value"]))
                                 for f in facts)
                return "recall-facts", "Stored facts:\n%s" % body
            return "recall-empty", "Nothing remembered yet."
        if intent == "schedule":
            return "queue-reminder", (
                "Reminder noted. Connect a scheduler (GitHub Actions cron) "
                "to deliver it — see docs/DEPLOY.md.")
        if intent == "report":
            return "report", ("Status: %d events ingested, %d runs executed. "
                              "Sovereign, offline, yours."
                              % (self.mem.count_events(), self.mem.count_runs()))
        if intent == "task":
            return "accept-task", (
                "Task accepted. The GitHub Actions runner executes approved "
                "tasks — approve from Telegram to run.")
        return "chat", safe(self.brain.draft(text, context))

    @staticmethod
    def _parse_kv(text):
        body = re.sub(r"^(?:remember|store|save|احفظ|تذكر)\s*:?\s*", "",
                      text, flags=re.I)
        m = re.search(r"([a-zA-Z؀-ۿ _-]{1,40})\s*[:=]\s*(.+)", body)
        if m:
            return (m.group(1).strip().lower().replace(" ", "_"),
                    m.group(2).strip())
        return None, None
