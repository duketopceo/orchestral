// orchestral observatory — hosted read-only mirror.
//
// The SPA and every api/*.json payload are rendered locally by harness.py
// sync and pushed here; this Worker is a thin Access-gated file server over
// R2. It does NOT contain the observatory's derivation logic — porting
// state.py would create the second engine the architecture forbids.
//
// Invariants enforced here (the sync client's allowlists are the first
// gate; this is the second):
//   - host check: only obs.shippedit.dev — the *.workers.dev route is off
//     in wrangler.toml AND rejected here, because it would bypass Access
//   - ingest requires the CF-Access-Jwt-Assertion header (service token)
//   - forbidden key names (call bodies, previews) are rejected recursively
//   - D1 writes are parameterized and column-allowlisted again server-side
//   - artifact bytes always carry the CSP sandbox header

const HOST = "obs.shippedit.dev";

// Access application binding for ingest JWT validation — the JWT's aud
// must match the app AUD tag and common_name must be the obs-ingest
// service token's client ID (env.INGEST_IDENTITY in wrangler.toml).
const ACCESS_TEAM = "duketopceo.cloudflareaccess.com";
const APP_AUD = "acd03e81af7da5f90210f58bdbac8edec8025f0c5736c5847934d715f7e4457f";

const READ_ONLY = {
  error: "hosted observatory is read-only",
  detail: "Launches, threads, flags, cancels, and estimates run locally via harness.py. This mirror only serves synced snapshots.",
};

const FORBIDDEN_KEYS = new Set([
  "input_json", "output_json", "call_previews",
]);

// Same projection allowlists as orchestral/cf.py — a body naming any other
// column fails ingest rather than being silently dropped.
const RUN_COLUMNS = [
  "run_id", "orchestrator", "task_id", "worker", "status",
  "started_at", "finished_at", "total_cost_usd",
  "total_input_tokens", "total_output_tokens", "score", "passes",
  "judge_score", "judge_passed", "latency_ms", "failure_reason",
  "run_group", "replicate", "dry_run", "delegated", "holdout",
];
const CALL_COLUMNS = [
  "call_id", "run_id", "phase", "step", "role", "model",
  "input_tokens", "output_tokens", "cost_usd", "api_cost_usd",
  "pricing_source", "latency_ms", "attempt", "error_category",
  "sequence", "worker_id", "dry_run", "created_at", "finish_reason",
];
const ANNOTATION_COLUMNS = ["kind", "target", "flag", "note", "updated_at"];

const ARTIFACT_TYPES = {
  html: "text/html; charset=utf-8",
  htm: "text/html; charset=utf-8",
  css: "text/css; charset=utf-8",
  js: "text/javascript; charset=utf-8",
  mjs: "text/javascript; charset=utf-8",
  json: "application/json",
  svg: "image/svg+xml",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  mp4: "video/mp4",
  webm: "video/webm",
  zip: "application/zip",
  pdf: "application/pdf",
  txt: "text/plain; charset=utf-8",
  md: "text/markdown; charset=utf-8",
};

// Model-authored HTML/JS must never execute in the observatory origin.
const ARTIFACT_HEADERS = {
  "Content-Security-Policy": "sandbox allow-scripts",
  "X-Content-Type-Options": "nosniff",
};

function json(obj, status = 200, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

function readOnly() {
  return json(READ_ONLY, 501);
}

function containsForbiddenKey(value) {
  if (value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(containsForbiddenKey);
  for (const [k, v] of Object.entries(value)) {
    if (FORBIDDEN_KEYS.has(k) || containsForbiddenKey(v)) return true;
  }
  return false;
}

const RUN_ID_RE = /^[A-Za-z0-9._-]+$/;
const KEY_RE = /^[A-Za-z0-9._\/%-]+$/;

function safeKey(key) {
  return typeof key === "string" && key.length < 512 &&
    KEY_RE.test(key) && !key.includes("..") && !key.startsWith("/");
}

async function r2json(env, key) {
  const obj = await env.BUCKET.get(key);
  if (obj === null) return null;
  return obj;
}

async function serveJson(env, key, transform) {
  const obj = await r2json(env, key);
  if (obj === null) return json({ error: `not found` }, 404);
  if (!transform) {
    return new Response(obj.body, {
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }
  const data = await obj.json();
  return json(transform(data));
}

async function serveArtifact(env, runId, member) {
  let key;
  if (member) {
    key = `runs/${runId}/artifact.members/${member}`;
  } else {
    // artifact.* — extension varies by task type; list the prefix
    const listed = await env.BUCKET.list({ prefix: `runs/${runId}/artifact.`, limit: 20 });
    const hit = listed.objects.find(o => !o.key.startsWith(`runs/${runId}/artifact.members/`));
    if (!hit) return json({ error: "no artifact" }, 404);
    key = hit.key;
  }
  const obj = await env.BUCKET.get(key);
  if (obj === null) return json({ error: "no artifact" }, 404);
  const ext = key.includes(".") ? key.split(".").pop().toLowerCase() : "";
  return new Response(obj.body, {
    headers: {
      "Content-Type": ARTIFACT_TYPES[ext] || "application/octet-stream",
      ...ARTIFACT_HEADERS,
    },
  });
}

// --- /api routing -----------------------------------------------------------

async function handleApi(env, url) {
  const p = url.pathname;
  const q = url.searchParams;

  // Endpoints that need live harness state or playwright — explicit 501
  // rather than a silent wrong answer.
  if (p === "/api/shot.png" || p === "/api/estimate" ||
      p === "/api/thread-estimate" || p === "/api/thread") {
    return readOnly();
  }

  if (p.startsWith("/api/run/")) {
    const parts = p.slice("/api/run/".length).split("/");
    const runId = parts[0];
    if (!RUN_ID_RE.test(runId)) return json({ error: "bad run id" }, 400);
    if (parts.length === 1) return serveJson(env, `api/run/${runId}.json`);
    if (parts[1] === "live") return serveJson(env, `api/run/${runId}/live.json`);
    if (parts[1] === "evidence") return serveJson(env, `api/run/${runId}/evidence.json`);
    if (parts[1] === "artifact") {
      const member = parts.length > 2
        ? decodeURIComponent(parts.slice(2).join("/")) : null;
      if (member && !safeKey(member)) return json({ error: "bad member" }, 400);
      return serveArtifact(env, runId, member);
    }
    return json({ error: `not found: ${p}` }, 404);
  }

  if (p === "/api/overview") {
    return serveJson(env, "api/overview.json",
      d => ({ ...d, hosted: true, read_only: true }));
  }
  if (p === "/api/experiment") {
    const name = q.get("matrix") || "jev-ab";
    if (!RUN_ID_RE.test(name)) {
      return json({ error: "matrix must be a spec name" }, 400);
    }
    return serveJson(env, `api/experiment.${name}.json`);
  }
  if (p === "/api/compare") {
    const a = q.get("a") || "", b = q.get("b") || "";
    if (!a || !b) return json({ error: "compare needs ?a=<group>&b=<group>" }, 400);
    const key = x => encodeURIComponent(x).replace(/[!'()*]/g,
      c => "%" + c.charCodeAt(0).toString(16).toUpperCase());
    const first = await r2json(env, `api/compare.${key(a)}.vs.${key(b)}.json`);
    if (first !== null) {
      return new Response(first.body, {
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    }
    return serveJson(env, `api/compare.${key(b)}.vs.${key(a)}.json`);
  }
  if (p === "/api/card") {
    const kind = q.get("kind") || "group", target = q.get("target") || "";
    if (!RUN_ID_RE.test(kind)) return json({ error: "bad kind" }, 400);
    const key = encodeURIComponent(target).replace(/[!'()*]/g,
      c => "%" + c.charCodeAt(0).toString(16).toUpperCase());
    return serveJson(env, `api/card.${kind}.${key}.json`);
  }
  if (p === "/api/runs") {
    // filters are applied here — the snapshot is the full row set
    const group = q.get("group"), task = q.get("task"),
      status = q.get("status"), needle = (q.get("q") || "").toLowerCase();
    return serveJson(env, "api/runs.json", rows => rows.filter(r =>
      (!group || r.run_group === group) &&
      (!task || r.task_id === task) &&
      (!status || r.status === status) &&
      (!needle || [r.run_id, r.task_id, r.orchestrator, r.worker]
        .some(v => (v || "").toLowerCase().includes(needle)))));
  }
  if (p === "/api/leaderboard") {
    const sort = q.get("sort") || "cost_per_pass";
    return serveJson(env, "api/leaderboard.json", rows =>
      Array.isArray(rows) && rows.length && sort in rows[0]
        ? [...rows].sort((a, b) => (a[sort] ?? 0) - (b[sort] ?? 0))
        : rows);
  }

  // flat snapshots: /api/<name> → api/<name>.json
  const name = p.slice("/api/".length);
  if (/^[A-Za-z0-9._-]+$/.test(name) && !name.includes("..")) {
    return serveJson(env, `api/${name}.json`);
  }
  return json({ error: `not found: ${p}` }, 404);
}

// --- ingest -----------------------------------------------------------------

function checkColumns(rows, allowed, table) {
  const ok = new Set(allowed);
  for (const row of rows) {
    for (const k of Object.keys(row)) {
      if (!ok.has(k)) {
        throw Object.assign(
          new Error(`${table}: column '${k}' is not in the projection allowlist`),
          { status: 400 });
      }
    }
  }
}

async function upsert(env, table, columns, rows) {
  if (!rows || rows.length === 0) return;
  checkColumns(rows, columns, table);
  const stmt = env.DB.prepare(
    `INSERT OR REPLACE INTO ${table} (${columns.join(",")}) ` +
    `VALUES (${columns.map(() => "?").join(",")})`);
  await env.DB.batch(
    rows.map(r => stmt.bind(...columns.map(c => r[c] ?? null))));
}

async function writeObjects(env, map, prefixRe) {
  const written = [];
  for (const [key, value] of Object.entries(map || {})) {
    if (!safeKey(key) || !prefixRe.test(key)) {
      throw Object.assign(
        new Error(`rejected object key '${key}'`), { status: 400 });
    }
    let body;
    if (typeof value === "string") {
      body = Uint8Array.from(atob(value), c => c.charCodeAt(0));
    } else {
      body = new TextEncoder().encode(JSON.stringify(value));
    }
    await env.BUCKET.put(key, body);
    written.push(key);
  }
  return written;
}

// --- Access JWT verification -------------------------------------------------
// Access signs Cf-Access-Jwt-Assertion RS256 with the team keys at
// /cdn-cgi/access/certs. Verify signature + exp + aud + service-token
// common_name here — presence alone proves nothing if a client-side header
// were ever forwarded unverified, and an aud/common_name pin blocks the
// app's other admitted identities (human logins) from the write path.

let jwksCache = { keys: null, exp: 0 };

function b64url(s) {
  return Uint8Array.from(
    atob(s.replace(/-/g, "+").replace(/_/g, "/")),
    c => c.charCodeAt(0));
}

async function accessKeys() {
  if (!jwksCache.keys || Date.now() > jwksCache.exp) {
    const r = await fetch(`https://${ACCESS_TEAM}/cdn-cgi/access/certs`);
    if (!r.ok) throw new Error("access certs fetch failed");
    const { keys = [] } = await r.json();
    const map = new Map();
    for (const k of keys) {
      if (k.kty === "RSA") {
        map.set(k.kid, await crypto.subtle.importKey(
          "jwk", k, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
          false, ["verify"]));
      }
    }
    jwksCache = { keys: map, exp: Date.now() + 3600e3 };
  }
  return jwksCache.keys;
}

async function verifyAccessJwt(assertion) {
  const parts = (assertion || "").split(".");
  if (parts.length !== 3) return null;
  try {
    const header = JSON.parse(new TextDecoder().decode(b64url(parts[0])));
    const key = (await accessKeys()).get(header.kid);
    if (!key) return null;
    const ok = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5", key, b64url(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
    if (!ok) return null;
    const claims = JSON.parse(new TextDecoder().decode(b64url(parts[1])));
    if (!claims.exp || claims.exp * 1000 < Date.now()) return null;
    const aud = claims.aud;
    if (!(Array.isArray(aud) ? aud.includes(APP_AUD) : aud === APP_AUD)) {
      return null;
    }
    return claims;
  } catch {
    return null;
  }
}

async function handleIngest(request, env, url) {
  if (request.method !== "POST") return json({ error: "POST only" }, 405);
  // Access (non_identity policy) validates the service token at the edge;
  // this verifies the assertion it issued and pins it to obs-ingest —
  // human logins allowed for viewing carry different common_names and
  // must not reach the write path.
  const claims = await verifyAccessJwt(
    request.headers.get("CF-Access-Jwt-Assertion"));
  if (!claims || claims.common_name !== env.INGEST_IDENTITY) {
    return json({ error: "ingest requires the Access service token" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }
  if (containsForbiddenKey(body)) {
    return json({ error: "payload contains a forbidden key (call bodies never sync)" }, 400);
  }

  const d1 = body.d1 || {};
  try {
    if (url.pathname === "/ingest/run") {
      const runId = body.run_id;
      if (!RUN_ID_RE.test(runId || "")) {
        return json({ error: "bad run_id" }, 400);
      }
      const esc = runId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const written = await writeObjects(
        env, body.payloads, new RegExp(`^api/run/${esc}(\\.json|/)`));
      const filesWritten = await writeObjects(
        env, body.files, new RegExp(`^runs/${esc}/`));
      if (body.manifest_hash != null) {
        if (typeof body.manifest_hash !== "string" ||
            !/^[0-9a-f]{64}$/.test(body.manifest_hash)) {
          return json({ error: "manifest_hash must be a sha256 hex digest" }, 400);
        }
        await env.BUCKET.put(`runs/${runId}/.manifest-hash`, body.manifest_hash);
      }
      await upsert(env, "runs", RUN_COLUMNS, d1.runs);
      await upsert(env, "calls", CALL_COLUMNS, d1.calls);
      return json({ ok: true, run_id: runId, written: written.length + filesWritten.length });
    }
    if (url.pathname === "/ingest/state") {
      const written = await writeObjects(env, body.payloads, /^api\//);
      await upsert(env, "runs", RUN_COLUMNS, d1.runs);
      await upsert(env, "calls", CALL_COLUMNS, d1.calls);
      await upsert(env, "annotations", ANNOTATION_COLUMNS, d1.annotations);
      return json({ ok: true, written: written.length });
    }
  } catch (e) {
    return json({ error: e.message }, e.status || 500);
  }
  return json({ error: `not found: ${url.pathname}` }, 404);
}

// --- entry ------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname !== HOST) {
      // workers_dev is off in wrangler.toml; this is the in-code belt — a
      // stray route must never serve the observatory outside Access.
      return new Response(`orchestral observatory lives at https://${HOST}\n`, { status: 421 });
    }
    if (url.pathname.startsWith("/ingest/")) {
      return handleIngest(request, env, url);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return readOnly();
    }
    if (url.pathname.startsWith("/api/")) {
      return handleApi(env, url);
    }
    // static SPA — same mapping the local server uses
    if (url.pathname === "/") {
      return env.ASSETS.fetch(new Request(new URL("/app.html", url), request));
    }
    if (url.pathname === "/favicon.ico") {
      return env.ASSETS.fetch(new Request(new URL("/favicon.svg", url), request));
    }
    if (url.pathname.startsWith("/static/")) {
      const asset = new URL(url.pathname.slice("/static".length), url);
      return env.ASSETS.fetch(new Request(asset, request));
    }
    if (["/runs", "/leaderboard", "/compare", "/new"].includes(url.pathname)) {
      return Response.redirect(`${url.origin}/#${url.pathname}`, 302);
    }
    if (url.pathname.startsWith("/run/")) {
      return Response.redirect(`${url.origin}/#${url.pathname}`, 302);
    }
    return json({ error: `not found: ${url.pathname}` }, 404);
  },
};
