// Nexus-Zero ingestion gateway — Cloudflare Worker (free tier, ~100k req/day).
//
// Role: receive webhooks, verify the right secret for each route, and wake the
// GitHub Actions runner via repository_dispatch. It holds NO state and NO
// server — the edge is free and your data passes through only to reach YOUR
// runner.
//
// Two routes, two secrets (Telegram cannot send custom headers):
//   POST /telegram  -> guarded by X-Telegram-Bot-Api-Secret-Token (set at
//                      setWebhook via `secret_token`). Verified here.
//   POST /ingest    -> guarded by X-Nexus-Secret (your own callers).
//
// Hardened after audit:
//   * constant-time secret compare (hash-then-fold, no length leak).
//   * /telegram checks the Telegram secret TOKEN HEADER (the old body check
//     was dead code — Telegram never sends it in the body).
//   * returns 503 when the dispatch fails, so Telegram's built-in webhook
//     retry becomes the retransmit layer (at-least-once at the edge).
//
// Deploy (free):
//   npm i -g wrangler && wrangler login
//   wrangler secret put NEXUS_GATEWAY_SECRET      (>= 24 chars)
//   wrangler secret put TELEGRAM_WEBHOOK_SECRET   (same as setWebhook secret_token)
//   wrangler secret put GH_OWNER / GH_REPO / GH_DISPATCH_TOKEN
//   wrangler deploy
//
// Set the webhook:
//   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
//     -d '{"url":"https://<worker>/telegram","secret_token":"<TELEGRAM_WEBHOOK_SECRET>"}'

const encoder = new TextEncoder();
const MIN_SECRET_LEN = 24;

// constant-time compare of two strings (hash to equal length, then XOR-fold)
async function timingSafeEqual(a, b) {
  if (!a || !b) return false;
  const ha = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(a)));
  const hb = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(b)));
  if (ha.length !== hb.length) return false;
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha[i] ^ hb[i];
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST") return json({ ok: false, err: "POST only" }, 405);

    if (url.pathname === "/telegram") {
      // Telegram sends the secret as a HEADER, not in the body.
      const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
      if (!strong(env.TELEGRAM_WEBHOOK_SECRET)) return weak("TELEGRAM_WEBHOOK_SECRET");
      if (!(await timingSafeEqual(got, env.TELEGRAM_WEBHOOK_SECRET))) {
        return json({ ok: false, err: "bad telegram secret" }, 403);
      }
    } else if (url.pathname === "/ingest") {
      const got = request.headers.get("X-Nexus-Secret") || "";
      if (!strong(env.NEXUS_GATEWAY_SECRET)) return weak("NEXUS_GATEWAY_SECRET");
      if (!(await timingSafeEqual(got, env.NEXUS_GATEWAY_SECRET))) {
        return json({ ok: false, err: "bad secret" }, 403);
      }
    } else {
      return json({ ok: false, err: "unknown route" }, 404);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ ok: false, err: "bad json" }, 400);
    }

    // wake the free runner. Retryable failure -> 503 (sender retries);
    // PERMANENT failure (revoked token, bad repo) -> 500 so we don't spin.
    const dispatched = await wakeRunner(env, url.pathname, payload);
    if (dispatched === true) return json({ ok: true, routed: url.pathname, dispatched }, 202);
    if (dispatched === "permanent") {
      return json({ ok: false, err: "dispatch misconfigured (check GH token/repo)" }, 500);
    }
    return json({ ok: false, err: "runner wake failed; retry" }, 503);
  },
};

const strong = (s) => typeof s === "string" && s.length >= MIN_SECRET_LEN;
const weak = (name) =>
  json({ ok: false, err: `${name} unset or < ${MIN_SECRET_LEN} chars (fail closed)` }, 500);

async function wakeRunner(env, route, payload) {
  if (!env.GH_OWNER || !env.GH_REPO || !env.GH_DISPATCH_TOKEN) return false;
  const resp = await fetch(
    `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "nexus-ingest",
        client_payload: { route, payload },
      }),
    }
  );
  return resp.status === 204
    ? true
    : [401, 403, 404, 422].includes(resp.status)
      ? "permanent" // revoked token / bad repo / oversized payload: retry won't help
      : false;      // transient: let the sender retry (503)
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
