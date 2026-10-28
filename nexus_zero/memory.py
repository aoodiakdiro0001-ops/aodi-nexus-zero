"""Nexus-Zero memory: relational + full-text, hardened stdlib SQLite.

Honest scope (v1.0.0): this store is LOCAL SQLite. The `NEXUS_DB` DSN swap
promised in v1.0.0 was false — sqlite3.connect() cannot open a libsql:// or
https:// URL, so pointing NEXUS_DB at Turso silently created a junk file. We
now FAIL LOUDLY on any URL/DSN instead of pretending. True cloud memory needs
an HTTP libsql client (a dependency) — bring your own adapter behind this
interface, or persist the local file (the Actions workflow commits it).

Hardening after a security+architecture audit (+ a focused review pass):
  * WAL + busy_timeout + token claims -> concurrent ticks never double-process.
  * ABANDONED-CLAIM RECLAIM: a crash after `handled=2` no longer strands the
    row forever; stale claims are released after CLAIM_TTL and retried.
  * events: raw_id dedupe (rowcount-detected) + attempts/counter + dead-letter.
  * messages: per-event idempotency (event_id + unique index) so a retried
    event can't durably duplicate conversation history or skew FTS rank.
  * facts/messages/history/search are THREAD-scoped (no cross-chat leakage),
    and search() can EXCLUDE the row just written (no self-match on recall).
  * FTS5 correlated on rowid; `rank` referenced by real table name (an alias
    in ORDER BY raises "no such column: f" and silently degraded to LIKE).
  * file chmod 0600 ALWAYS (a checked-out DB is 0644 under the runner umask);
    ANY scheme:// DSN is rejected (not just libsql/http/https).
  * outbox reaches a terminal state (sent=2) on exhaustion instead of
    silently dropping the reply with no trace.
"""
import os
import re
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  source TEXT NOT NULL,
  sender TEXT,
  text TEXT NOT NULL,
  raw TEXT,
  raw_id TEXT,
  handled INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0,
  claim TEXT,
  claimed REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe
  ON events(source, raw_id) WHERE raw_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  thread TEXT NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  event_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_event
  ON messages(thread, role, event_id) WHERE event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS facts (
  thread TEXT NOT NULL DEFAULT 'default',
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL DEFAULT 1.0,
  updated REAL NOT NULL,
  PRIMARY KEY (thread, key)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  intent TEXT,
  action TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  chat_id TEXT,
  text TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  sent INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text);
"""

DEFAULT_DB = os.environ.get("NEXUS_DB", "nexus-zero.db")
MAX_ATTEMPTS = 5
CLAIM_TTL = 900          # a claim older than this is presumed abandoned
_URL_DSN = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _clamp_limit(limit):
    try:
        return max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return 8


class Memory(object):
    def __init__(self, path=None):
        self.path = os.path.expanduser(path or DEFAULT_DB)
        if _URL_DSN.match(self.path):
            raise ValueError(
                "NEXUS_DB must be a local SQLite path, not a URL/DSN; a remote "
                "libsql URL needs an HTTP client (not sqlite3.connect). Persist "
                "the local file or bring a libsql adapter.")
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        try:
            os.chmod(self.path, 0o600)          # ALWAYS, not just when fresh
        except OSError:
            pass
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Additive, idempotent column upgrades for DBs made by older builds."""
        def has(col, table):
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
            return col in cols
        if not has("claimed", "events"):
            self.conn.execute("ALTER TABLE events ADD COLUMN claimed REAL")
        if not has("event_id", "messages"):
            self.conn.execute("ALTER TABLE messages ADD COLUMN event_id INTEGER")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS messages_event "
                "ON messages(thread, role, event_id) WHERE event_id IS NOT NULL")

    # ------------------------------------------------------------- events
    def add_event(self, source, text, sender=None, raw=None, raw_id=None):
        """Enqueue a signal; returns id, or None if it is a source retry."""
        import json as _json
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events "
            "(ts, source, sender, text, raw, raw_id) VALUES (?,?,?,?,?,?)",
            (time.time(), source, sender, str(text),
             _json.dumps(raw, ensure_ascii=False) if raw else None,
             str(raw_id) if raw_id is not None else None))
        # rowcount==0 means the unique index ignored it -> a source retry.
        # (lastrowid would be a stale id here, so we must NOT trust it.)
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    def claim(self, n=50):
        """Atomically claim up to n pending events (marks them in-progress).

        Reclaims stale claims first so a crashed tick can't strand rows. Token
        + timestamp based so two concurrent ticks never claim the same row.
        """
        now = time.time()
        # release rows abandoned by a dead/crashed tick (handled=2, stale)
        self.conn.execute(
            "UPDATE events SET handled=0, claim=NULL "
            "WHERE handled=2 AND COALESCE(claimed, 0) < ?", (now - CLAIM_TTL,))
        token = uuid.uuid4().hex
        self.conn.execute(
            "UPDATE events SET handled=2, claim=?, claimed=? WHERE id IN "
            "(SELECT id FROM events WHERE handled=0 AND attempts<? "
            "ORDER BY id LIMIT ?)", (token, now, MAX_ATTEMPTS, _clamp_limit(n)))
        rows = self.conn.execute(
            "SELECT id, source, sender, text FROM events "
            "WHERE claim=? AND handled=2", (token,)).fetchall()
        return rows

    def pending_events(self, limit=50):
        return self.conn.execute(
            "SELECT id, source, sender, text FROM events WHERE handled=0 "
            "ORDER BY id LIMIT ?", (_clamp_limit(limit),)).fetchall()

    def mark_handled(self, event_id):
        self.conn.execute("UPDATE events SET handled=1 WHERE id=?", (event_id,))

    def bump_attempt(self, event_id):
        """Count a failure; dead-letter the event once it exhausts retries."""
        self.conn.execute(
            "UPDATE events SET attempts=attempts+1, handled="
            "CASE WHEN attempts+1>=? THEN 3 ELSE 0 END, claim=NULL WHERE id=?",
            (MAX_ATTEMPTS, event_id))

    # ----------------------------------------------------------- messages
    def add_message(self, thread, role, text, event_id=None):
        """Idempotent per (thread, role, event_id): a retried event won't
        duplicate the row. Returns the message rowid (None if ignored)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(ts, thread, role, text, event_id) VALUES (?,?,?,?,?)",
            (time.time(), thread, role, text, event_id))
        if cur.rowcount == 0:
            return None                       # duplicate retry; skip FTS too
        self.conn.execute(
            "INSERT INTO messages_fts (rowid, text) VALUES (?,?)",
            (cur.lastrowid, text))
        return cur.lastrowid

    def history(self, thread, limit=10):
        rows = self.conn.execute(
            "SELECT role, text FROM messages WHERE thread=? ORDER BY id DESC "
            "LIMIT ?", (thread, _clamp_limit(limit))).fetchall()
        return list(reversed(rows))

    # -------------------------------------------------------------- facts
    def set_fact(self, key, value, thread="default", confidence=1.0):
        self.conn.execute(
            "INSERT INTO facts (thread, key, value, confidence, updated) "
            "VALUES (?,?,?,?,?) ON CONFLICT(thread, key) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, "
            "updated=excluded.updated",
            (thread, key, value, confidence, time.time()))

    def get_fact(self, key, thread="default"):
        row = self.conn.execute(
            "SELECT value, confidence FROM facts WHERE thread=? AND key=?",
            (thread, key)).fetchone()
        return {"value": row[0], "confidence": row[1]} if row else None

    def list_facts(self, thread="default", limit=5):
        rows = self.conn.execute(
            "SELECT key, value FROM facts WHERE thread=? "
            "ORDER BY updated DESC LIMIT ?", (thread, _clamp_limit(limit))).fetchall()
        return [{"key": k, "value": v} for k, v in rows]

    # ---------------------------------------------------------- full-text
    def search(self, query, limit=8, thread=None, exclude_id=None):
        """FTS recall, thread-scoped. Correlated on rowid (not content).

        exclude_id lets the caller drop the row just written so a `recall`
        doesn't match its own question (FTS5 implicit-AND self-match).
        """
        q = " ".join(re.findall(r"\w+", str(query))) or str(query)
        limit = _clamp_limit(limit)
        # NB: fts5 rank/rowid can't be reached through a table ALIAS in ORDER BY
        # (SQLite raises "no such column: f"), so reference the real table name.
        sql = ("SELECT m.role, m.text FROM messages_fts "
               "JOIN messages m ON m.id = messages_fts.rowid "
               "WHERE messages_fts MATCH ?")
        args = [q]
        if thread:
            sql += " AND m.thread = ?"
            args.append(thread)
        if exclude_id is not None:
            sql += " AND m.id <> ?"
            args.append(exclude_id)
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit)
        try:
            rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
            lsql = "SELECT role, text FROM messages WHERE text LIKE ? ESCAPE '\\'"
            largs = [like]
            if thread:
                lsql += " AND thread = ?"
                largs.append(thread)
            if exclude_id is not None:
                lsql += " AND id <> ?"
                largs.append(exclude_id)
            lsql += " ORDER BY id DESC LIMIT ?"
            largs.append(limit)
            rows = self.conn.execute(lsql, largs).fetchall()
        return [{"role": r, "text": t} for r, t in rows]

    # --------------------------------------------------------------- runs
    def log_run(self, intent, action, detail=""):
        self.conn.execute(
            "INSERT INTO runs (ts, intent, action, detail) VALUES (?,?,?,?)",
            (time.time(), intent, action, str(detail)[:500]))

    def count_events(self):
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def count_runs(self):
        return self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    # -------------------------------------------------------------- outbox
    def outbox_add(self, chat_id, text):
        self.conn.execute(
            "INSERT INTO outbox (ts, chat_id, text) VALUES (?,?,?)",
            (time.time(), chat_id, text))

    def outbox_pending(self, limit=50):
        return self.conn.execute(
            "SELECT id, chat_id, text, attempts FROM outbox "
            "WHERE sent=0 AND attempts<? ORDER BY id LIMIT ?",
            (MAX_ATTEMPTS, _clamp_limit(limit))).fetchall()

    def outbox_mark(self, out_id, ok):
        if ok:
            self.conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (out_id,))
        else:
            # terminal state (sent=2) once exhausted so drops are visible
            self.conn.execute(
                "UPDATE outbox SET attempts=attempts+1, sent="
                "CASE WHEN attempts+1>=? THEN 2 ELSE 0 END WHERE id=?",
                (MAX_ATTEMPTS, out_id))

    def close(self):
        self.conn.close()
