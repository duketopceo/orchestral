class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body || {};
  }
}

// One plain sentence for any failed request: the server's own message
// (first line only, so a stray traceback never reaches the page), else a
// description of the status instead of a bare code.
function readableError(status, body) {
  const msg = typeof body?.error === "string" ? body.error.split("\n")[0].trim() : "";
  if (msg) return msg;
  if (status === 0) return "Could not reach the observatory server. Check that it is still running, then try again.";
  if (status === 404) return "That item was not found. It may have been moved or deleted.";
  if (status === 403) return "The server refused this request. Reload the page and try again.";
  if (status >= 500) return "The observatory server hit an error. Check the server terminal for details.";
  return `The request failed (HTTP ${status}).`;
}

export async function api(path, opts) {
  let r;
  try { r = await fetch(path, opts); }
  catch { throw new ApiError(readableError(0, {}), 0, {}); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(readableError(r.status, body), r.status, body);
  return body;
}
