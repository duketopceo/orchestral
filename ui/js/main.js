/* orchestral observatory entry point: hash-routed SPA over /api/*.
   Two verdict axes are kept visually distinct everywhere:
   mechanical pass = green/red, judge score = info blue. */
import { loadMeta } from "./data.js";
import { route } from "./router.js";
import { startRail } from "./live.js";
import { initShell } from "./shell.js";
import { initKeys } from "./keys.js";
import { watchTables } from "./components/table.js";
import { initStatus } from "./status.js";
import { $view } from "./dom.js";

const meta = await loadMeta();
document.documentElement.dataset.mode = meta.mode;
await initShell();
initKeys();
watchTables($view);
initStatus(document.getElementById("status-line"), meta);
window.addEventListener("hashchange", route);
startRail();
route();
