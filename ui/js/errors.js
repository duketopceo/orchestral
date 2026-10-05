/* Error reporting to GlitchTip (errors.pacehq.io) via the Sentry
   envelope protocol. Reports only from the production observatory
   origin; local development and third-party clones stay silent.
   The DSN key is ingest-only (can write events, never read), so it
   is safe to embed in this public repository. A GitHub Actions poller
   turns new GlitchTip issues into scrubbed GitHub issues. */
const DSN = "https://95e8b32350134fb395bd203a80bcfb0f@errors.pacehq.io/16";
const ORIGIN = "https://obs.shippedit.dev";
const MAX_EVENTS = 5;
const seen = new Set();
let sent = 0;

const [key, host, project] = [
  DSN.split("@")[0].slice(8),
  DSN.split("@")[1].split("/")[0],
  DSN.split("/").pop(),
];
const ENDPOINT = `https://${host}/api/${project}/envelope/?sentry_key=${key}&sentry_version=7`;

function frames(stack) {
  if (!stack) return [];
  const out = [];
  for (const line of stack.split("\n")) {
    const m = line.match(/<?([^\s()<>]+)>?:(\d+):(\d+)\)?\s*$/);
    if (!m) continue;
    const [, file, lineno, colno] = m;
    out.push({
      filename: file.split("/").pop(),
      abs_path: file,
      lineno: +lineno,
      colno: +colno,
      in_app: !file.includes("node_modules"),
    });
  }
  return out.reverse().slice(-20);
}

function report(error, source) {
  if (sent >= MAX_EVENTS) return;
  const type = error && error.name ? error.name : "Error";
  const value = error && error.message ? String(error.message) : String(error);
  const fr = frames(error && error.stack);
  const site = fr.at(-1);
  const fp = `${type}|${value}|${site ? site.filename + ":" + site.lineno : source}`;
  if (seen.has(fp)) return;
  seen.add(fp);
  sent += 1;
  const id = crypto.randomUUID().replaceAll("-", "");
  const event = {
    event_id: id,
    timestamp: new Date().toISOString(),
    platform: "javascript",
    level: "error",
    logger: "observatory",
    transaction: location.pathname + location.hash.split("?")[0],
    exception: {
      values: [
        { type, value: value.slice(0, 2000), stacktrace: { frames: fr } },
      ],
    },
    tags: { mode: document.documentElement.dataset.mode || "unknown" },
    contexts: {
      browser: { name: navigator.userAgent.split(" ").pop() },
    },
  };
  const envelope = `${JSON.stringify({ event_id: id, sent_at: event.timestamp })}\n${JSON.stringify({ type: "event" })}\n${JSON.stringify(event)}`;
  fetch(ENDPOINT, {
    method: "POST",
    keepalive: true,
    headers: { "Content-Type": "application/x-sentry-envelope" },
    body: envelope,
  }).catch(() => {});
}

export function initErrors() {
  if (location.origin !== ORIGIN) return;
  window.addEventListener("error", (e) => report(e.error || e.message, "onerror"));
  window.addEventListener("unhandledrejection", (e) => report(e.reason, "rejection"));
}
