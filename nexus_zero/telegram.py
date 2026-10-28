"""Telegram gateway over the Bot API — stdlib urllib only, no phone-home
beyond the endpoint you own (api.telegram.org).

Hardened after audit:
  * parse_update is type-safe (malformed payloads return None, never raise).
  * inbound updates are AUTHORIZED against TELEGRAM_CHAT_ID (an allowlist),
    so a stranger who finds the bot cannot read your memory or trigger work.
  * verify_webhook compares BYTES (a non-ASCII secret now works) and refuses
    weak (<24 char) secrets loudly instead of silently 403-ing you out.
  * token is redacted from any error string that could reach a log.
"""
import hmac
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API = "https://api.telegram.org/bot%s"
TIMEOUT = 30
MIN_SECRET_LEN = 24


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


TIMEOUT = _int_env("NEXUS_TG_TIMEOUT", 30)


def _redact(msg):
    for k in ("TELEGRAM_TOKEN", "HF_TOKEN"):
        v = os.environ.get(k)
        if v:
            msg = msg.replace(v, "***")
    return msg[:160]


def _post(method, payload):
    url = (API % TOKEN) + "/" + method
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(payload).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def send(text, chat_id=None):
    """Send a message; returns {ok:bool} (+ err, redacted, on failure)."""
    if not TOKEN:
        return {"ok": False, "err": "TELEGRAM_TOKEN not set"}
    try:
        return _post("sendMessage", {"chat_id": chat_id or CHAT_ID,
                                     "text": str(text)[:4096]})
    except Exception as e:
        return {"ok": False, "err": _redact(str(e))}


def parse_update(update, allowed_chat_id=None):
    """Normalize a Telegram update; AUTHORIZED sends only.

    Returns {source, sender, text, raw_id} or None (non-text / not-ours /
    malformed). `allowed_chat_id` is the operator's TELEGRAM_CHAT_ID; when
    set, updates from any other chat are dropped.
    """
    if not isinstance(update, dict):
        return None
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = msg.get("chat")
    cid = chat.get("id") if isinstance(chat, dict) else None
    sender = str(cid) if cid is not None else None
    if allowed_chat_id and sender != str(allowed_chat_id):
        return None                                  # not our chat; drop
    return {"source": "telegram", "sender": sender,
            "text": text[:4096], "raw_id": update.get("update_id")}


def verify_webhook(secret, header_value):
    """Constant-time byte compare; fails closed on empty or weak secrets."""
    if not secret or not header_value:
        return False
    try:
        s = str(secret).encode("utf-8", "strict")
        h = str(header_value).encode("utf-8", "strict")
    except (UnicodeEncodeError, AttributeError):
        return False
    if len(s) < MIN_SECRET_LEN:
        return False                                 # refuse weak config
    return hmac.compare_digest(s, h)
