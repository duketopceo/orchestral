// Local end-to-end harness ONLY (wrangler.e2e.toml; never deployed). Wraps the
// production Worker unchanged and adds /__seed so scripts/e2e-hosted.py can load
// a rendered snapshot into the local (Miniflare) R2 bucket without Access: the
// real /ingest path verifies an Access JWT against Cloudflare's certs, which a
// local run must never contact. Seeded keys still pass the ingest key grammar.
import worker from "../worker.js";
import { ingestKeyKind } from "../keys.js";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/__seed" && request.method === "POST") {
      const { objects } = await request.json(); // {key: {b64} | {json}}
      let n = 0;
      for (const [key, v] of Object.entries(objects)) {
        if (!ingestKeyKind(key)) return new Response(`rejected ${key}`, { status: 400 });
        const body = v.b64 !== undefined
          ? Uint8Array.from(atob(v.b64), c => c.charCodeAt(0))
          : new TextEncoder().encode(JSON.stringify(v.json));
        await env.BUCKET.put(key, body);
        n++;
      }
      return new Response(JSON.stringify({ seeded: n }), { headers: { "Content-Type": "application/json" } });
    }
    return worker.fetch(request, env, ctx);
  },
};
