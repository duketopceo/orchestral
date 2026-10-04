// Key grammar for the hosted observatory's R2 tree.
//
// orchestral/web/snapshot.py is the only writer of api/ keys; this module is
// the Worker's mirror of its shapes and nothing wider. A request path
// /api/<rest> maps to the object api/<rest>.json, and <rest> must be one of:
//
//   <name>                      meta, overview, runs, groups, matrix, leaderboard,
//                               flags, pairings, models-catalog, experiments,
//                               experiment.<name>, pairings.<group>, cards.<lens>,
//                               compare.<a>.<b>
//   run/<id>                    run detail
//   run/<id>/live               live payload
//   run/<id>/evidence           evidence payload
//   card/<kind>/<target>.<lens> one report card
//
// Every segment is percent-encoded text (snapshot.py enc(), ui/js/data.js
// keyEnc()): letters, digits and `._~-`, or %XX escapes. No empty segment, no
// `.`/`..`, no `..` substring, no leading slash, bounded length.

export const MAX_KEY_LENGTH = 400;
export const MAX_SEGMENT_LENGTH = 300;

const SEGMENT_RE = /^(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+$/;

export function isSafeSegment(seg) {
  if (typeof seg !== "string" || seg.length === 0 || seg.length > MAX_SEGMENT_LENGTH ||
      !SEGMENT_RE.test(seg) || seg === "." || seg.includes("..")) return false;
  try {
    const decoded = decodeURIComponent(seg);
    return decoded !== "." && !decoded.includes("..") && !decoded.includes("\0");
  } catch {
    return false; // %XX that is not valid UTF-8
  }
}

// The part of an /api/ path (after "/api/") to its payload key, without the
// "api/" prefix or ".json" suffix; null when it is not a snapshot key shape.
export function payloadName(rest) {
  if (typeof rest !== "string" || rest.length === 0 || rest.length > MAX_KEY_LENGTH) return null;
  const parts = rest.split("/");
  if (!parts.every(isSafeSegment)) return null;
  const [head, , third] = parts;
  switch (parts.length) {
    case 1: return head === "run" || head === "card" ? null : rest;
    case 2: return head === "run" ? rest : null;
    case 3:
      if (head === "run" && (third === "live" || third === "evidence")) return rest;
      return head === "card" ? rest : null;
    default: return null;
  }
}

// Object keys the ingest path may write: api/<payloadName>.json payloads, or
// runs/<id>/<file> scrubbed artifact files (members nest one level deeper).
export function ingestKeyKind(key) {
  if (typeof key !== "string" || key.length === 0 || key.length > MAX_KEY_LENGTH) return null;
  if (key.startsWith("api/")) {
    if (!key.endsWith(".json")) return null;
    return payloadName(key.slice("api/".length, -".json".length)) ? "payload" : null;
  }
  if (key.startsWith("runs/")) {
    const parts = key.split("/");
    return parts.length >= 3 && parts.length <= 6 && parts.every(isSafeSegment) ? "file" : null;
  }
  return null;
}
