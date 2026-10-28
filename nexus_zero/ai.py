"""Inference adapter: one interface, three free backends.

  NEXUS_AI=stub   deterministic offline brain (tests + demo, zero network)
  NEXUS_AI=ollama local models at localhost:11434 (free, sovereign)
  NEXUS_AI=hf     Hugging Face serverless router (free tier, needs HF_TOKEN)

Every backend answers classify/draft/embed. A rule-based intent floor backs
all three, so a weak or offline model never breaks classification.

Hardened after audit: env ints parsed safely (a bad NEXUS_AI_TIMEOUT can no
longer kill `doctor`), redirects are NOT followed (an endpoint 302 can't leak
the Authorization header to another host), response reads are capped, and
provider error text is redacted before it can reach a log or a reply.
"""
import json
import os
import re
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_MODEL = os.environ.get("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
HF_URL = "https://router.huggingface.co/v1/chat/completions"
MAX_READ = 4 * 1024 * 1024


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


TIMEOUT = _int_env("NEXUS_AI_TIMEOUT", 60)

# rule table — the offline brain and a floor for every online backend too
# order matters: recall is checked before remember so "what do you remember"
# is a retrieval, not a store.
INTENTS = [
    ("recall",   (r"\brecall\b", r"\bwhat do you (?:know|remember)\b", r"ما تعرفه"), "retrieve memory"),
    ("remember", (r"\b(?:remember|store|save|احفظ|تذكر)\b",), "store a fact"),
    ("schedule", (r"\b(?:schedule|remind|cron|جدول|ذكّر|تذكير)\b",), "set a reminder"),
    ("report",   (r"\b(?:report|summary|digest|تقرير|لخّص|ملخص)\b",), "produce a report"),
    ("task",     (r"\b(?:run|execute|deploy|build|نفّ|شغّ|انشر|ابنِ)\b",), "run a task"),
    ("chat",     (r".*",), "general conversation"),
]

_ALL_INTENTS = {i[0] for i in INTENTS}


def _rule_classify(text):
    low = str(text).lower()
    for intent, patterns, _desc in INTENTS:
        for pat in patterns:
            if re.search(pat, low):
                return {"intent": intent,
                        "confidence": 0.9 if intent != "chat" else 0.4}
    return {"intent": "chat", "confidence": 0.4}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_json(url, payload, headers):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with _OPENER.open(req, timeout=TIMEOUT) as r:
        body = r.read(MAX_READ)
    return json.loads(body.decode("utf-8"))


def _redact(msg):
    for k in ("TELEGRAM_TOKEN", "HF_TOKEN"):
        v = os.environ.get(k)
        if v:
            msg = msg.replace(v, "***")
    return msg[:160]


class StubBrain(object):
    """Deterministic, offline, zero-network. Used in tests and `doctor`."""

    name = "stub"

    def classify(self, text):
        return _rule_classify(text)

    def draft(self, text, context=None):
        c = _rule_classify(text)
        ack = {"remember": "Saved to memory.",
               "recall": "Here is what I recall.",
               "schedule": "Reminder queued.",
               "report": "Report compiled.",
               "task": "Task accepted.",
               "chat": "Understood."}.get(c["intent"], "Understood.")
        return "%s [intent=%s]" % (ack, c["intent"])

    def embed(self, text):
        return None


class OllamaBrain(object):
    name = "ollama"

    def classify(self, text):
        floor = _rule_classify(text)
        try:
            out = self._chat([{"role": "system", "content":
                               "Classify the user message into exactly one of: "
                               "remember, recall, schedule, report, task, chat. "
                               "Reply with the single word only."},
                              {"role": "user", "content": str(text)}])
            word = out.strip().lower().split()[0] if out.strip() else ""
            if word in _ALL_INTENTS:
                return {"intent": word, "confidence": max(floor["confidence"], 0.8)}
        except Exception:
            pass
        return floor

    def draft(self, text, context=None):
        msgs = [{"role": "system", "content":
                 "You are a concise sovereign operations assistant. Be brief, "
                 "honest, and act only on what the user asked."}]
        for m in (context or [])[-6:]:
            msgs.append({"role": m["role"], "content": m["text"]})
        msgs.append({"role": "user", "content": str(text)})
        try:
            return self._chat(msgs).strip() or "(no output)"
        except Exception as e:
            return "(inference unavailable)"      # detail only at debug

    def embed(self, text):
        try:
            r = _post_json(OLLAMA_URL + "/api/embeddings",
                           {"model": OLLAMA_MODEL, "prompt": str(text)}, {})
            return r.get("embedding")
        except Exception:
            return None

    def _chat(self, messages):
        r = _post_json(OLLAMA_URL + "/api/chat",
                       {"model": OLLAMA_MODEL, "messages": messages,
                        "stream": False}, {})
        return r.get("message", {}).get("content", "")


class HFBrain(object):
    name = "huggingface"

    def classify(self, text):
        floor = _rule_classify(text)
        try:
            out = self._chat([{"role": "system", "content":
                               "Classify into one word: remember, recall, "
                               "schedule, report, task, chat."},
                              {"role": "user", "content": str(text)}])
            word = out.strip().lower().split()[0] if out.strip() else ""
            if word in _ALL_INTENTS:
                return {"intent": word, "confidence": 0.8}
        except Exception:
            pass
        return floor

    def draft(self, text, context=None):
        msgs = [{"role": "system", "content":
                 "You are a concise sovereign operations assistant."}]
        for m in (context or [])[-6:]:
            msgs.append({"role": m["role"], "content": m["text"]})
        msgs.append({"role": "user", "content": str(text)})
        try:
            return self._chat(msgs).strip() or "(no output)"
        except Exception:
            return "(inference unavailable)"

    def embed(self, text):
        return None

    def _chat(self, messages):
        if not HF_TOKEN:
            raise RuntimeError("HF_TOKEN not set")
        r = _post_json(HF_URL,
                       {"model": HF_MODEL, "messages": messages,
                        "max_tokens": 512},
                       {"Authorization": "Bearer " + HF_TOKEN})
        return r["choices"][0]["message"]["content"]


def get_brain(kind=None):
    kind = (kind or os.environ.get("NEXUS_AI", "stub")).lower()
    if kind == "ollama":
        return OllamaBrain()
    if kind in ("hf", "huggingface"):
        return HFBrain()
    return StubBrain()
