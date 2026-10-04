/* orchestral observatory entry point: hash-routed SPA over /api/*.
   Two verdict axes are kept visually distinct everywhere:
   mechanical pass = green/red, judge score = info blue. */
import { route } from "./router.js";
import { refreshJobs } from "./rail.js";

window.addEventListener("hashchange", route);
route();
refreshJobs();
setInterval(refreshJobs, 5000);
