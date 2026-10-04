// node --test infra/cloudflare/observatory/test
import assert from "node:assert/strict";
import { test } from "node:test";
import { ingestKeyKind, payloadName } from "../keys.js";
import worker from "../worker.js";

const OK = [
  "meta", "meta.json", "overview", "runs", "groups", "matrix", "leaderboard", "flags",
  "pairings", "models-catalog", "experiments", "experiment.jev-ab",
  "pairings.group-a", "cards.overall", "cards.cost",
  "compare.a.b", "compare.g%2F1.g%20two", "compare.caf%C3%A9.b",
  "run/20260101-abc_1", "run/r.1/live", "run/r.1/evidence",
  "card/group/g1.overall", "card/pairing/orch%2Fx__w%2Fy.cost",
];
const BAD = [
  "", "/", "/run/x", "run", "run/", "run//x", "run/x/", "run/../x", "../x", "a/../b",
  "run/x/live/extra", "run/x/artifact", "run/x/other", "card/group", "card/a/b/c",
  "unknown/x", "x/y", "a..b", "%2e%2e", "run/%2e%2e/live", "bad%zz", "bad%2", "sp ace",
  "q?x=1", "a#b", "back\\slash", "nul%00".replace("%00", "\0"), "x".repeat(401),
  `run/${"y".repeat(301)}`, ".", "run/./live",
];

test("accepts exactly the snapshot key shapes", () => {
  for (const k of OK) assert.equal(payloadName(k), k, k);
});

test("rejects unsafe or unknown shapes", () => {
  for (const k of BAD) assert.equal(payloadName(k), null, JSON.stringify(k));
  assert.equal(payloadName(undefined), null);
  assert.equal(payloadName(null), null);
});

test("ingest keys: api payloads and runs files only", () => {
  assert.equal(ingestKeyKind("api/compare.a.b.json"), "payload");
  assert.equal(ingestKeyKind("api/run/r1/live.json"), "payload");
  assert.equal(ingestKeyKind("api/run/r1.json"), "payload");
  assert.equal(ingestKeyKind("runs/r1/report.json"), "file");
  assert.equal(ingestKeyKind("runs/r1/artifact.members/a/b.js"), "file");
  for (const k of ["api/x", "api/../x.json", "/api/x.json", "runs/r1", "runs/../x/y", "other/x.json",
    "api/run/r1/other.json", "runs/r1/../../x", "runs//x/y"]) {
    assert.equal(ingestKeyKind(k), null, k);
  }
});

function env(objects = {}) {
  const seen = [];
  return {
    seen,
    BUCKET: {
      async get(key) {
        seen.push(key);
        return key in objects
          ? { body: JSON.stringify(objects[key]), json: async () => objects[key] } : null;
      },
      async list() { return { objects: [] }; },
    },
    ASSETS: { fetch: async req => new Response(`asset:${new URL(req.url).pathname}`) },
  };
}
const call = (e, path, init) =>
  worker.fetch(new Request(`https://obs.shippedit.dev${path}`, init), e);

test("worker maps /api/<key> to api/<key>.json and ignores the query", async () => {
  const e = env({ "api/compare.a.b.json": { ok: 1 }, "api/run/r1/live.json": { rows: [] } });
  const r = await call(e, "/api/compare.a.b?x=1");
  assert.equal(r.status, 200);
  assert.deepEqual(JSON.parse(await r.text()), { ok: 1 });
  assert.equal((await call(e, "/api/run/r1/live?after=3")).status, 200);
  assert.equal((await call(e, "/api/compare.a.zz")).status, 404);
});

test("worker meta.json fallback and bad keys", async () => {
  const e = env({ "api/meta.json": { mode: "hosted" } });
  assert.equal((await call(e, "/api/meta")).status, 200);
  assert.equal((await call(e, "/api/meta.json")).status, 200);
  const before = e.seen.length;
  for (const p of ["/api/..%2Fx", "/api/a/b/c/d", "/api/run/x/y", "/api/", "/api/run//live"]) {
    assert.equal((await call(e, p)).status, 400, p);
  }
  assert.equal(e.seen.length, before, "bad keys never reach R2");
});

test("worker stays read-only and host-checked", async () => {
  const e = env();
  assert.equal((await call(e, "/api/runs", { method: "POST", body: "{}" })).status, 501);
  assert.equal((await call(e, "/api/shot.png")).status, 501);
  assert.equal((await call(e, "/api/tasks")).status, 501);
  const other = await worker.fetch(new Request("https://x.workers.dev/api/runs"), e);
  assert.equal(other.status, 421);
  const ing = await call(e, "/ingest/state", { method: "POST", body: "{}" });
  assert.equal(ing.status, 401);
});

test("artifacts keep the sandbox CSP", async () => {
  const e = env();
  e.BUCKET.get = async () => ({ body: "<h1>x</h1>" });
  const r = await call(e, "/api/run/r1/artifact/index.html");
  assert.equal(r.headers.get("Content-Security-Policy"), "sandbox allow-scripts");
  assert.equal((await call(e, "/api/run/r1/artifact/..%2Fx")).status, 400);
});
