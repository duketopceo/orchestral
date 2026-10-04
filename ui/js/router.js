import { $view } from "./dom.js";
import { stopPolling } from "./poller.js";
import { esc } from "./util.js";
import { viewAbout } from "./views/about.js";
import { viewCard } from "./views/card.js";
import { viewCards } from "./views/cards.js";
import { viewCompare } from "./views/compare.js";
import { viewLeaderboard } from "./views/leaderboard.js";
import { viewModels } from "./views/models.js";
import { viewNew } from "./views/new.js";
import { viewOverview } from "./views/overview.js";
import { viewRun } from "./views/run.js";
import { viewRuns } from "./views/runs.js";

export async function route() {
  stopPolling();
  const hash = location.hash.slice(1) || "/";
  const [pathQ, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const path = pathQ || "/";

  // detail routes light up their parent section, not nothing
  const parent = path.startsWith("/run/") ? "/runs"
    : path === "/card" ? "/cards"
    : path;
  document.querySelectorAll("#nav a").forEach(a => {
    a.classList.toggle("active",
      a.dataset.route === "/" ? parent === "/" : parent.startsWith(a.dataset.route));
  });

  delete $view.dataset.ready;
  try {
    if (path === "/") await viewOverview();
    else if (path === "/runs") await viewRuns(params);
    else if (path.startsWith("/run/")) await viewRun(path.split("/")[2], params);
    else if (path === "/compare") await viewCompare(params);
    else if (path === "/leaderboard") await viewLeaderboard(params);
    else if (path === "/cards") await viewCards(params);
    else if (path === "/card") await viewCard(params);
    else if (path === "/models") await viewModels();
    else if (path === "/new") await viewNew();
    else if (path === "/about") await viewAbout();
    else $view.innerHTML = `<div class="empty">Unknown view: ${esc(path)}</div>`;
  } catch (e) {
    $view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
  // settled marker for headless captures (/api/shot.png)
  $view.dataset.ready = "1";
}
