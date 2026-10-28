"""Nexus-Zero CLI — init, ingest, tick, doctor, remember, recall, serve.

stdlib-only. Every command is offline-safe; network is used only by the
brain/telegram you explicitly configure.

The `serve` receiver is hardened: it AUTHENTICATES before reading the body,
caps the body size, sets a socket timeout (slow-loris), and authorizes inbound
updates against TELEGRAM_CHAT_ID. Binding off-loopback warns about cleartext.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus_zero import __version__, workflow  # noqa: E402
from nexus_zero import telegram as tg  # noqa: E402
from nexus_zero.memory import Memory  # noqa: E402

MAX_BODY = 64 * 1024


def cmd_init(args):
    mem = Memory(args.db)
    path = mem.path
    mem.close()
    print("initialized memory at %s" % path)
    return 0


def cmd_ingest(args):
    eid = workflow.ingest(args.text, source=args.source, sender=args.sender,
                          db=args.db)
    print("queued event #%s" % eid if eid else "duplicate (ignored)")
    return 0


def cmd_tick(args):
    summary = workflow.tick(db=args.db, notify=not args.no_notify,
                            log=None if args.quiet else print)
    if args.json:
        print(json.dumps({"processed": summary["processed"],
                          "failed": summary.get("failed", 0),
                          "at": summary["at"]}, indent=2))
    return 0


def cmd_doctor(args):
    out, ok = workflow.doctor(db=args.db)
    print(out)
    return 0 if ok else 1


def cmd_remember(args):
    mem = Memory(args.db)
    mem.set_fact(args.key, args.value)
    mem.close()
    print("remembered: %s" % args.key)          # value not echoed (may be secret)
    return 0


def cmd_recall(args):
    mem = Memory(args.db)
    limit = max(1, min(int(args.limit), 100))
    hits = mem.search(args.query, limit=limit) if args.query else []
    if not hits:
        hits = [{"role": "fact", "text": "%s: %s" % (f["key"], f["value"])}
                for f in mem.list_facts(limit=limit)]
    mem.close()
    from nexus_zero.core import safe
    for h in hits:
        print("- [%s] %s" % (h["role"], safe(h["text"])))
    if not hits:
        print("(nothing remembered yet)")
    return 0


def cmd_serve(args):
    """Minimal local webhook receiver: auth-first, capped, chat-authorized."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    secret = os.environ.get("NEXUS_GATEWAY_SECRET", "")
    allowed = os.environ.get("TELEGRAM_CHAT_ID") or None

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("WARNING: binding %s exposes X-Nexus-Secret over plain HTTP; "
              "terminate TLS in front." % args.host, file=sys.stderr)
    if len(secret) < tg.MIN_SECRET_LEN:
        print("WARNING: NEXUS_GATEWAY_SECRET is unset or shorter than %d chars"
              % tg.MIN_SECRET_LEN, file=sys.stderr)

    class H(BaseHTTPRequestHandler):
        timeout = 15
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            # authenticate BEFORE touching the body
            if not tg.verify_webhook(secret, self.headers.get("X-Nexus-Secret")):
                self.close_connection = True
                return self._json(403, {"ok": False, "err": "bad secret"})
            try:
                ln = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"ok": False, "err": "bad length"})
            if ln <= 0 or ln > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"ok": False, "err": "body too large"})
            body = self.rfile.read(ln)
            try:
                update = json.loads(body.decode("utf-8"))
            except (ValueError, RecursionError):
                self.close_connection = True
                return self._json(400, {"ok": False, "err": "bad json"})
            ev = tg.parse_update(update, allowed_chat_id=allowed)
            if ev is None:
                self.close_connection = True
                return self._json(403, {"ok": False,
                                        "err": "not an authorized text update"})
            eid = workflow.ingest(ev["text"], source=ev["source"],
                                  sender=ev["sender"], raw=update,
                                  raw_id=ev.get("raw_id"), db=args.db)
            return self._json(202 if eid else 200,
                              {"ok": True, "event": eid})

    httpd = ThreadingHTTPServer((args.host, args.port), H)
    httpd.request_queue_size = 16
    print("webhook on http://%s:%d (auth-first, capped, chat-authorized)"
          % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nexus-zero", description=__doc__)
    ap.add_argument("--db", default=None, help="memory path (or NEXUS_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the memory store")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("ingest", help="queue a signal")
    p.add_argument("text")
    p.add_argument("--source", default="cli")
    p.add_argument("--sender", default=None)
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("tick", help="process pending signals (the cron job)")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("doctor", help="environment self-check")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("remember", help="store a long-term fact")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(fn=cmd_remember)
    p = sub.add_parser("recall", help="search memory / list facts")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(fn=cmd_recall)
    p = sub.add_parser("serve", help="local auth-first webhook receiver")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9500)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
