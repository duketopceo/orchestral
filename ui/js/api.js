/* Transport: one fetch wrapper, one error type. Knows nothing about routes or
   modes; data.js decides which URL a resource lives at. */

export class ApiError extends Error {
  constructor(message, status, body, extra = {}) {
    super(message);
    this.status = status;
    this.body = body || {};
    // true when fetch itself failed (offline, DNS, or an expired Access session)
    this.network = !!extra.network;
  }
}

// One plain sentence for any failed request: the server's own message
// (first line only, so a stray traceback never reaches the page), else a
// description of the status instead of a bare code.
export function readableError(status, body) {
  const msg = typeof body?.error === "string" ? body.error.split("\n")[0].trim() : "";
  if (msg) return msg;
  if (status === 0) return "Could not reach the observatory server. Check that it is still running, then try again.";
  if (status === 404) return "That item was not found. It may have been moved or deleted.";
  if (status === 403) return "The server refused this request. Reload the page and try again.";
  if (status >= 500) return "The observatory server hit an error. Check the server terminal for details.";
  return `The request failed (HTTP ${status}).`;
}

export const isAbort = e => e?.name === "AbortError";

// Worth retrying: the request never completed, the server errored, or it
// asked us to slow down. A 4xx the server meant is final.
export function isTransient(e) {
  if (!(e instanceof ApiError)) return false;
  return e.status === 0 || e.status === 429 || (e.status >= 500 && e.status !== 501);
}

export async function api(path, opts) {
  let r;
  try { r = await fetch(path, opts); }
  catch (e) {
    if (isAbort(e)) throw e;
    throw new ApiError(readableError(0, {}), 0, {}, { network: true });
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(readableError(r.status, body), r.status, body);
  return body;
}
