/* Hash router. route() renders the current view, marks #view[data-ready] "ok"
   or "error", retries transient failures after 1s, 2s and 4s before offering a
   manual Retry, and aborts a superseded render's requests so a stale response
   never paints over a newer one. */
import { $view } from "./dom.js";
import { ApiError, isAbort, isTransient } from "./api.js";
import { can, isHosted, setSignal } from "./data.js";
import { stopScope } from "./poller.js";
import { setConnection } from "./status.js";
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

export const RETRY_DELAYS_MS = [1000, 2000, 4000];

// Routes that need a capability the hosted mirror does not have.
const LOCAL_ONLY = { "/new": "launch" };

let current = null; // AbortController of the render in flight

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    }, { once: true });
  });
}

function dispatch(path, params) {
  if (LOCAL_ONLY[path] && !can(LOCAL_ONLY[path])) return renderUnavailable();
  if (path === "/") return viewOverview();
  if (path === "/runs") return viewRuns(params);
  if (path.startsWith("/run/")) return viewRun(decodeURIComponent(path.split("/")[2]), params);
  if (path === "/compare") return viewCompare(params);
  if (path === "/leaderboard") return viewLeaderboard(params);
  if (path === "/cards") return viewCards(params);
  if (path === "/card") return viewCard(params);
  if (path === "/models") return viewModels();
  if (path === "/new") return viewNew();
  if (path === "/about") return viewAbout();
  $view.innerHTML = `<div class="empty">Unknown view: ${esc(path)}</div>`;
}

function renderUnavailable() {
  $view.innerHTML = `
    <div class="empty rest-state" data-rest="r-missing" id="not-available">
      <h1>Not available on this read-only build</h1>
      <p>This page launches or changes runs, which only the local observatory can do.
         Run <code>python harness.py serve</code> on the machine that holds the runs.</p>
      <p><a class="btn" href="#/">Go to Now</a></p>
    </div>`;
}

function renderError(e) {
  const signIn = isHosted() && e.network
    ? `<button type="button" class="btn" data-act="signin">Sign in again</button>` : "";
  const meaning = e.status === 404
    ? "Nothing is wrong with the page; the item may be outside this data set."
    : e.network ? "The request did not reach the server, so nothing here is out of date or lost."
    : "The request reached the server but did not succeed.";
  $view.innerHTML = `
    <div class="empty rest-state state-error" data-rest="r-error" role="alert">
      <h1>This view could not load</h1>
      <p>${esc(e.message)}</p>
      <p class="dim">${esc(meaning)}</p>
      <p><button type="button" class="primary" id="retry">Retry</button> ${signIn}
         <a class="btn" href="#/">Go to Now</a></p>
    </div>`;
  document.getElementById("retry").addEventListener("click", () => route());
}

function markNav(path) {
  // detail routes light up their parent section, not nothing
  const parent = path.startsWith("/run/") ? "/runs" : path === "/card" ? "/cards" : path;
  document.querySelectorAll("#nav a").forEach(a => {
    a.classList.toggle("active",
      a.dataset.route === "/" ? parent === "/" : parent.startsWith(a.dataset.route));
  });
}

export async function route() {
  stopScope("route");
  current?.abort();
  const ctl = current = new AbortController();
  setSignal(ctl.signal);

  const hash = location.hash.slice(1) || "/";
  const [pathQ, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const path = pathQ || "/";
  markNav(path);
  delete $view.dataset.ready;

  for (let attempt = 0; ; attempt++) {
    try {
      await dispatch(path, params);
      if (ctl.signal.aborted) return;
      setConnection("ok");
      $view.dataset.ready = "ok";
      return;
    } catch (e) {
      if (ctl.signal.aborted || isAbort(e)) return;
      if (isTransient(e) && attempt < RETRY_DELAYS_MS.length) {
        setConnection("reconnecting");
        try { await sleep(RETRY_DELAYS_MS[attempt], ctl.signal); } catch { return; }
        continue;
      }
      if (isTransient(e)) setConnection("offline");
      renderError(e instanceof ApiError ? e : new ApiError(e?.message || "Something went wrong while drawing this view.", -1, {}));
      $view.dataset.ready = "error";
      return;
    }
  }
}
