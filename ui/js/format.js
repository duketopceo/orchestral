/* One formatting contract (KTD3). Mirrors orchestral/format.py; both are driven
   by tests/fixtures/format_cases.json. Display only: nothing here changes data. */

export const NULL_GLYPH = "-";
export const LOW_N_CELL = 3;
export const LOW_N_BEST = 10;
const SLUG_MAX = 36;
const SLUG_TAIL = 14;

// toFixed rounds half up on the exact value; sign is kept only if a digit survives.
function fixed(v, places) {
  const s = Math.abs(v).toFixed(places);
  return v < 0 && Number(s) !== 0 ? `-${s}` : s;
}
const roundHalfUp = v => Math.floor(v + 0.5);

export function money(v) {
  if (v == null) return NULL_GLYPH;
  if (v < 0) return `-${money(-v)}`;
  if (v === 0) return "$0.00";
  if (v < 0.00005) return "<$0.0001";
  if (v < 0.01) {
    const s = fixed(v, 4);
    if (Number(s) < 0.01) return `$${s}`;
  }
  if (v < 1) {
    const s = fixed(v, 3);
    if (Number(s) < 1) return `$${s}`;
  }
  const [whole, frac] = fixed(v, 2).split(".");
  return `$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${frac}`;
}

export function percent(v) { return v == null ? NULL_GLYPH : `${roundHalfUp(v * 100)}%`; }
export function score(v) { return v == null ? NULL_GLYPH : fixed(v, 2); }

export function rangePct(lo, hi) {
  if (lo == null || hi == null) return NULL_GLYPH;
  return `${roundHalfUp(lo * 100)}-${roundHalfUp(hi * 100)}%`;
}

export function duration(ms) {
  if (ms == null || ms < 0) return NULL_GLYPH;
  if (ms < 1000) return `${roundHalfUp(ms)}ms`;
  if (ms < 60000 && Number(fixed(ms / 1000, 1)) < 60) return `${fixed(ms / 1000, 1)}s`;
  const secs = roundHalfUp(ms / 1000);
  const pad = n => String(n).padStart(2, "0");
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${pad(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${pad(Math.floor((secs % 3600) / 60))}m`;
}

export function tokens(n) {
  if (n == null) return NULL_GLYPH;
  if (n < 1000) return String(roundHalfUp(n));
  if (n < 999950) return `${fixed(n / 1000, 1)}k`;
  return `${fixed(n / 1000000, 1)}M`;
}

export function delta(v, unit) {
  if (v == null) return NULL_GLYPH;
  if (unit === "pp") {
    const n = roundHalfUp(v * 100);
    return `${n > 0 ? "+" : ""}${n}pp`;
  }
  if (unit === "score") {
    const s = fixed(v, 2);
    return Number(s) > 0 ? `+${s}` : s;
  }
  if (unit === "money") return v > 0 ? `+${money(v)}` : money(v);
  throw new Error(`unknown delta unit: ${unit}`);
}

export function shortSlug(slug, maxLen = SLUG_MAX) {
  if (!slug) return NULL_GLYPH;
  const name = String(slug).split("/").pop();
  if (name.length <= maxLen) return name;
  return `${name.slice(0, maxLen - 1 - SLUG_TAIL)}…${name.slice(-SLUG_TAIL)}`;
}

export function lowNCell(n) { return n == null || n < LOW_N_CELL; }
export function lowNBest(n) { return n == null || n < LOW_N_BEST; }
