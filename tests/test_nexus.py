"""Nexus-Zero offline test suite — stub brain, temp SQLite, fake Telegram.
Zero network. Includes security regression tests pinning the audit fixes.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus_zero import ai as ai_mod
from nexus_zero import telegram as tg
from nexus_zero import workflow
from nexus_zero.core import Nexus, safe
from nexus_zero.memory import Memory

STRONG = "a-very-long-gateway-secret-32chars!!"


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "mem.db")


class MemoryTests(unittest.TestCase):
    def test_events_claim_and_handled(self):
        m = Memory(_tmp())
        m.add_event("cli", "hello", sender="u1")
        rows = m.claim()
        self.assertEqual(len(rows), 1)
        m.mark_handled(rows[0][0])
        self.assertEqual(len(m.claim()), 0)
        m.close()

    def test_raw_id_dedupe(self):
        m = Memory(_tmp())
        a = m.add_event("telegram", "hi", raw_id="42")
        b = m.add_event("telegram", "hi", raw_id="42")   # source retry
        self.assertIsNotNone(a)
        self.assertIsNone(b)                              # ignored duplicate
        m.close()

    def test_remote_dsn_rejected(self):
        for dsn in ("libsql://x.turso.io", "https://x.turso.io"):
            with self.assertRaises(ValueError):
                Memory(dsn)

    def test_facts_thread_scoped(self):
        m = Memory(_tmp())
        m.set_fact("password", "secretA", thread="alice")
        m.set_fact("password", "secretB", thread="bob")
        self.assertEqual(m.get_fact("password", thread="alice")["value"], "secretA")
        self.assertEqual(m.get_fact("password", thread="bob")["value"], "secretB")
        self.assertIsNone(m.get_fact("password", thread="eve"))   # isolation
        m.close()

    def test_search_is_real_fts_not_like(self):
        # words present but NOT contiguous -> LIKE would miss; FTS must hit
        m = Memory(_tmp())
        m.add_message("u1", "user", "the deploy password policy is strict")
        hits = m.search("strict deploy")                 # non-contiguous query
        self.assertTrue(any("deploy" in h["text"] for h in hits))
        m.close()

    def test_search_thread_scoped(self):
        m = Memory(_tmp())
        m.add_message("alice", "user", "alice secret token one")
        m.add_message("bob", "user", "bob secret token two")
        hits = m.search("secret token", thread="alice")
        self.assertTrue(all("alice" in h["text"] or "token" in h["text"]
                            for h in hits))
        self.assertEqual(len(hits), 1)                   # bob's row excluded
        m.close()

    def test_history_chronological(self):
        m = Memory(_tmp())
        m.add_message("u1", "user", "first")
        m.add_message("u1", "assistant", "second")
        self.assertEqual([t for _r, t in m.history("u1", limit=5)],
                         ["first", "second"])
        m.close()

    def test_outbox_roundtrip(self):
        m = Memory(_tmp())
        m.outbox_add("777", "hello")
        rows = m.outbox_pending()
        self.assertEqual(len(rows), 1)
        m.outbox_mark(rows[0][0], ok=True)
        self.assertEqual(len(m.outbox_pending()), 0)
        m.close()

    def test_limit_clamped(self):
        m = Memory(_tmp())
        m.add_message("u1", "user", "x")
        self.assertEqual(len(m.search("x", limit=-5)), 1)   # clamped to >=1
        m.close()


class BrainTests(unittest.TestCase):
    def test_stub_classify_rules(self):
        b = ai_mod.StubBrain()
        self.assertEqual(b.classify("please remember language: arabic")["intent"], "remember")
        self.assertEqual(b.classify("what do you recall about deploy")["intent"], "recall")
        self.assertEqual(b.classify("schedule a reminder for 5pm")["intent"], "schedule")
        self.assertEqual(b.classify("give me a report")["intent"], "report")
        self.assertEqual(b.classify("run the build")["intent"], "task")
        self.assertEqual(b.classify("hello there")["intent"], "chat")

    def test_get_brain_fallback_stub(self):
        os.environ["NEXUS_AI"] = "nonexistent"
        self.assertEqual(ai_mod.get_brain().name, "stub")
        os.environ["NEXUS_AI"] = "stub"

    def test_bad_timeout_env_does_not_crash(self):
        os.environ["NEXUS_AI_TIMEOUT"] = "not-a-number"
        import importlib
        importlib.reload(ai_mod)                        # must not raise
        self.assertEqual(ai_mod.TIMEOUT, 60)
        os.environ["NEXUS_AI_TIMEOUT"] = "60"


class CoreTests(unittest.TestCase):
    def test_remember_then_recall(self):
        m = Memory(_tmp())
        n = Nexus(m, brain=ai_mod.StubBrain())
        r1 = n.handle_event(1, "cli", "u1", "remember language: arabic")
        self.assertEqual(r1["action"], "store-fact")
        self.assertEqual(m.get_fact("language", thread="u1")["value"], "arabic")
        r2 = n.handle_event(2, "cli", "u1", "what do you remember about language")
        self.assertIn("recall", r2["action"])
        m.close()

    def test_report_counts_runs(self):
        m = Memory(_tmp())
        n = Nexus(m, brain=ai_mod.StubBrain())
        n.handle_event(1, "cli", "u1", "hello")
        r = n.handle_event(2, "cli", "u1", "give me a report")
        self.assertEqual(r["action"], "report")
        self.assertIn("events ingested", r["reply"])
        m.close()

    def test_chat_uses_brain_draft(self):
        m = Memory(_tmp())
        n = Nexus(m, brain=ai_mod.StubBrain())
        r = n.handle_event(1, "cli", "u1", "hello there")
        self.assertEqual(r["intent"], "chat")
        self.assertIn("intent=chat", r["reply"])
        m.close()

    def test_cross_thread_isolation(self):
        m = Memory(_tmp())
        n = Nexus(m, brain=ai_mod.StubBrain())
        n.handle_event(1, "cli", "alice", "remember password: secretA")
        # bob recalls; alice's fact must not leak into bob's thread
        r = n.handle_event(2, "cli", "bob", "what do you remember about password")
        self.assertNotIn("secretA", r["reply"])
        m.close()

    def test_safe_strips_control_chars(self):
        # strips the ESC/BEL control bytes so the OSC escape can't fire; the
        # printable "]0;pwned" remains (harmless without its escape byte).
        self.assertEqual(safe("hi\x1b]0;pwned\x07 there"), "hi]0;pwned there")
        self.assertEqual(safe("tab\there\nnewline"), "tab\there\nnewline")


class WorkflowTests(unittest.TestCase):
    def test_ingest_and_tick_end_to_end(self):
        db = _tmp()
        workflow.ingest("remember name: baran", source="cli", db=db)
        s = workflow.tick(db=db, notify=False, log=None)
        self.assertEqual(s["processed"], 1)
        m = Memory(db)
        self.assertEqual(m.get_fact("name")["value"], "baran")
        self.assertEqual(len(m.claim()), 0)              # drained
        m.close()

    def test_tick_idempotent_on_empty_queue(self):
        self.assertEqual(workflow.tick(db=_tmp(), notify=False, log=None)["processed"], 0)

    def test_poison_event_isolated_and_dead_lettered(self):
        db = _tmp()

        class Boom(object):
            name = "boom"

            def classify(self, text):
                if "bad1" in str(text):
                    raise RuntimeError("boom")
                return {"intent": "chat", "confidence": 0.5}

            def draft(self, text, context=None):
                return "ok"

            def embed(self, text):
                return None

        workflow.ingest("bad1", source="cli", db=db)
        workflow.ingest("good2", source="cli", db=db)
        # first pass: bad raises (isolated), good still processes
        s = workflow.tick(db=db, brain=Boom(), notify=False, log=None)
        self.assertEqual(s["processed"], 1)
        self.assertEqual(s["failed"], 1)
        # after MAX_ATTEMPTS the poison event is dead-lettered (handled=3)
        for _ in range(5):
            workflow.tick(db=db, brain=Boom(), notify=False, log=None)
        m = Memory(db)
        rows = m.conn.execute(
            "SELECT handled FROM events WHERE text='bad1'").fetchone()
        self.assertEqual(rows[0], 3)                     # dead-lettered
        m.close()

    def test_doctor_reports_readiness(self):
        out, ok = workflow.doctor(db=_tmp())
        self.assertIn("Nexus-Zero doctor", out)
        self.assertIn("chat_allowlist", out)


class TelegramTests(unittest.TestCase):
    def test_parse_update_normalizes(self):
        ev = tg.parse_update({"update_id": 7,
                              "message": {"text": "hi", "chat": {"id": 123}}})
        self.assertEqual(ev["source"], "telegram")
        self.assertEqual(ev["sender"], "123")
        self.assertEqual(ev["raw_id"], 7)

    def test_parse_update_type_safe(self):
        self.assertIsNone(tg.parse_update({"message": "not-a-dict"}))
        self.assertIsNone(tg.parse_update({"message": {"text": ["a"], "chat": {}}}))
        self.assertIsNone(tg.parse_update({"update_id": 1, "message": {}}))

    def test_chat_allowlist_drops_strangers(self):
        upd = {"message": {"text": "hi", "chat": {"id": 999}}}
        self.assertIsNone(tg.parse_update(upd, allowed_chat_id="123"))
        upd2 = {"message": {"text": "hi", "chat": {"id": 123}}}
        self.assertIsNotNone(tg.parse_update(upd2, allowed_chat_id="123"))

    def test_webhook_secret_strong_and_nonascii(self):
        self.assertTrue(tg.verify_webhook(STRONG, STRONG))
        self.assertFalse(tg.verify_webhook(STRONG, STRONG + "x"))
        # non-ASCII secret now validates (byte compare, not str compare)
        ar = "سر-طويل-جدا-للمصادقة-1234567890"
        self.assertTrue(tg.verify_webhook(ar, ar))

    def test_weak_or_empty_secret_rejected(self):
        self.assertFalse(tg.verify_webhook("abc", "abc"))     # too short
        self.assertFalse(tg.verify_webhook("", ""))           # empty


class EndToEndTests(unittest.TestCase):
    def test_full_loop_with_fake_telegram_send(self):
        db = _tmp()
        sent = []
        orig = tg.send
        tg.send = lambda text, chat_id=None: sent.append((chat_id, text)) or {"ok": True}
        try:
            eid = workflow.ingest("remember city: istanbul", source="telegram",
                                  sender="777", db=db)
            self.assertIsNotNone(eid)
            workflow.tick(db=db, notify=True, log=None)
        finally:
            tg.send = orig
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "777")
        self.assertIn("Remembered", sent[0][1])


class SecurityRegressionTests(unittest.TestCase):
    """Pin the focused-review fixes so they can't silently regress."""

    def test_any_scheme_dsn_rejected(self):
        # postgres/mysql/redis DSNs (not just libsql/https) must be refused
        for dsn in ("postgres://user:pa55@host/db",
                    "mysql://root@localhost/x",
                    "redis://:pw@localhost/0"):
            with self.assertRaises(ValueError):
                Memory(dsn)

    def test_abandoned_claim_is_reclaimed(self):
        import time as _time
        m = Memory(_tmp())
        m.add_event("cli", "orphan", sender="u1")
        rows = m.claim()                       # marks handled=2, claimed=now
        self.assertEqual(len(rows), 1)
        # simulate a crash: backdate the claim past CLAIM_TTL
        m.conn.execute("UPDATE events SET claimed=? WHERE handled=2",
                       (_time.time() - 100000,))
        again = m.claim()                      # must reclaim, not strand
        self.assertEqual(len(again), 1)
        m.close()

    def test_search_excludes_recent_row(self):
        m = Memory(_tmp())
        mid = m.add_message("u1", "user", "alpha beta gamma")
        hits = m.search("alpha beta", exclude_id=mid)
        self.assertEqual(hits, [])             # own row is dropped
        hits2 = m.search("alpha beta")         # without exclude it matches
        self.assertEqual(len(hits2), 1)
        m.close()

    def test_message_idempotent_per_event(self):
        m = Memory(_tmp())
        m.add_message("u1", "user", "hello", event_id=7)
        m.add_message("u1", "user", "hello", event_id=7)   # retried event
        n = m.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE event_id=7").fetchone()[0]
        self.assertEqual(n, 1)                 # no duplicate
        m.close()

    def test_outbox_exhaustion_is_terminal(self):
        m = Memory(_tmp())
        m.outbox_add("777", "hi")
        oid = m.outbox_pending()[0][0]
        for _ in range(6):
            m.outbox_mark(oid, ok=False)       # keep failing
        self.assertEqual(len(m.outbox_pending()), 0)   # no longer pending
        sent = m.conn.execute("SELECT sent FROM outbox WHERE id=?",
                              (oid,)).fetchone()[0]
        self.assertEqual(sent, 2)              # terminal dead-letter, not silent
        m.close()

    def test_recall_does_not_echo_own_question(self):
        m = Memory(_tmp())
        n = Nexus(m, brain=ai_mod.StubBrain())
        n.handle_event(1, "cli", "u1", "remember capital: ankara")
        # a recall query should surface the FACT, not quote the question back
        r = n.handle_event(2, "cli", "u1", "what is the capital")
        self.assertNotIn("what is the capital", r["reply"])
        m.close()

    def test_exception_log_is_sanitized(self):
        # a provider error embedding a token must not reach the log verbatim
        os.environ["TELEGRAM_TOKEN"] = "SECRET_TOKEN_VALUE"
        try:
            import nexus_zero.workflow as wf
            from nexus_zero.core import safe as _safe
            msg = wf.tg._redact(_safe(Exception("boom SECRET_TOKEN_VALUE")))
            self.assertNotIn("SECRET_TOKEN_VALUE", msg)
        finally:
            del os.environ["TELEGRAM_TOKEN"]


if __name__ == "__main__":
    unittest.main()
