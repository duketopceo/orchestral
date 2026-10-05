/* Motion that explains a state change and nothing else (DESIGN.md 6.7).
   Only transform and opacity move. Everything here is a CSS class or the Web
   Animations API: no timers. With prefers-reduced-motion every helper does its
   state change at once and starts nothing. */

const MICRO = 120; // ms, same as --dur-micro
const EASE_OUT = "cubic-bezier(.2, 0, 0, 1)";

export function reduced() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const captureMode = () => document.documentElement.dataset.capture === "1";

/* Route change: 120ms crossfade of the view. Captures never fade. */
export function crossfade(el) {
  if (reduced() || captureMode() || typeof el.animate !== "function") return;
  el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: MICRO, easing: EASE_OUT });
}

/* Number tick: only the digits that changed slide in (120ms, vertical). The
   shared prefix stays put, so a cost going $0.0418 to $0.0431 moves "31". */
export function tick(el, text) {
  const next = String(text);
  const prev = el.textContent;
  if (prev === next) return;
  let i = 0;
  while (i < prev.length && i < next.length && prev[i] === next[i]) i++;
  if (reduced() || i === next.length) { el.textContent = next; return; }
  el.textContent = next.slice(0, i);
  const changed = document.createElement("span");
  changed.className = "tk";
  changed.textContent = next.slice(i);
  el.append(changed);
  changed.animate(
    [{ transform: "translateY(60%)", opacity: 0 }, { transform: "translateY(0)", opacity: 1 }],
    { duration: MICRO, easing: EASE_OUT });
}

/* Row insert: the class carries a 4px entry and a --live wash that fades over
   1.2s (CSS keyframes row-in and row-wash). Removed when the wash ends. */
function flag(nodes, cls, doneName) {
  if (reduced()) return;
  for (const n of nodes) {
    n.classList.add(cls);
    n.addEventListener("animationend", e => {
      if (e.animationName === doneName) n.classList.remove(cls);
    });
  }
}

export const enterRows = nodes => flag(nodes, "row-new", "row-wash");

/* Phase advance: a lane bar that just appeared fills left to right over 200ms. */
export const fillBars = nodes => flag(nodes, "ln-new", "ph-fill");

/* Replace a container's markup on a refresh and animate only what is new.
   `keyOf` names a node, `animate` is how the caller marks arrivals (enterRows or fillBars). */
export function patch(host, html, selector, keyOf, animate) {
  const before = new Set([...host.querySelectorAll(selector)].map(keyOf));
  host.innerHTML = html;
  animate([...host.querySelectorAll(selector)].filter(n => !before.has(keyOf(n))));
}

/* Skeleton rows match the final table row height (--row-h), so the page does
   not move when data arrives. Hidden for the first 120ms by CSS, so a fast
   route never shows it. */
export function skeleton(rows = 8) {
  return `<div class="skeleton" aria-hidden="true"><div class="skel-title"></div>
    <div class="skel-panel">${'<div class="skel-row"></div>'.repeat(rows)}</div></div>`;
}
