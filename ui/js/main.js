/* orchestral observatory entry point: hash-routed SPA over /api/*.
   Two verdict axes are kept visually distinct everywhere:
   mechanical pass = green/red, judge score = info blue. */
import { loadMeta } from "./data.js";
import { route } from "./router.js";
import { startRail } from "./rail.js";
import { initStatus } from "./status.js";

const meta = await loadMeta();
document.documentElement.dataset.mode = meta.mode;
initStatus(document.getElementById("status-line"), meta);
if (meta.mode === "hosted") document.getElementById("nav")?.querySelector(".nav-cta")?.remove();
window.addEventListener("hashchange", route);
startRail();
route();
