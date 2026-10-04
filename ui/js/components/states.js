/* Icons and the five states. Every glyph is a <use> into the sprite that
   shell.js injects once; text always sits beside it, so the glyph is
   aria-hidden. Each state is a Rest, a title, one line and one action. */
import { esc } from "../util.js";

export function icon(name, cls = "") {
  return `<svg class="i${cls ? ` ${cls}` : ""}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
}

/* The live indicator: the baton ring while running, the `stalled` glyph (a
   different shape) once quiet. `still` freezes it (hosted snapshots). */
export function liveGlyph({ stalled = false, still = false } = {}) {
  return stalled
    ? icon("stalled", "job-glyph")
    : `<span class="live-ring${still ? " still" : ""}" aria-hidden="true">${icon("live")}</span>`;
}

export function rest(name) {
  return `<svg class="rest" width="64" height="64" viewBox="0 0 64 64" aria-hidden="true" focusable="false"><use href="#r-${name}"/></svg>`;
}

/* kind -> Rest drawing.
   TODO(#127): `starting` should use #r-starting (staff with a rising baton). Its
   drawing is pending the logo decision in orchestral PR #127, so the slot uses
   #r-empty until then. */
const RESTS = { empty: "empty", nomatch: "nomatch", starting: "empty", missing: "missing", error: "error" };
export const STATE_KINDS = Object.keys(RESTS);

/* action: { label, href } renders a link, { label, id } a button, and
   { label, command } a copyable command line. Exactly one action. */
export function stateHtml(kind, { title, body = "", action, heading = false, attrs = "", role = "" } = {}) {
  if (!RESTS[kind]) throw new Error(`unknown state: ${kind}`);
  const act = !action ? ""
    : action.href ? `<a class="btn" href="${esc(action.href)}">${esc(action.label)}</a>`
    : action.command ? `<code class="cmd">${esc(action.command)}</code>`
    : `<button type="button" class="btn" id="${esc(action.id)}">${esc(action.label)}</button>`;
  const h = heading ? "h1" : "p";
  return `<div class="state state-${kind}" data-state-kind="${kind}" data-rest="r-${RESTS[kind]}"${role ? ` role="${role}"` : ""}${attrs ? ` ${attrs}` : ""}>
    ${rest(RESTS[kind])}
    <${h} class="state-title">${esc(title)}</${h}>
    ${body ? `<p class="state-body">${body}</p>` : ""}
    ${act ? `<p class="state-act">${act}</p>` : ""}
  </div>`;
}
