/* Text alternatives shared by the kit: "8 of 10 (80%)" before the bare
   percentage (DESIGN.md 6.9 #2), all through the one formatter (KTD3). */
import * as F from "../format.js";

export function ofText(passed, finished, rate) {
  if (!finished) return "no finished runs";
  return `${passed} of ${finished} (${F.percent(rate ?? passed / finished)})`;
}

export function pairingName(row) {
  return `${F.shortSlug(row.orchestrator)} to ${F.shortSlug(row.worker)}`;
}
