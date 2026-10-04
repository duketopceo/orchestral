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
import { markNav } from "./shell.js";
import { stateHtml } from "./components/states.js";
import { viewExperiment } from "./views/experiment.js";
import { viewAbout } from "./views/about.js";
import { viewCard } from "./views/card.js";
import { viewCards } from "./views/cards.js";
import { viewCompare } from "./views/compare.js";
import { viewLeaderboard } from "./views/leaderboard.js";
import { viewModels } from "./views/models.js";
import { viewNew } from "./views/new.js";
import { viewNow } from "./views/now.js";
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
  if (path === "/") return viewNow();
  if (path === "/runs") return viewRuns(params);
  if (path.startsWith("/run/")) return viewRun(decodeURIComponent(path.split("/")[2]), params);
  if (path === "/compare") return viewCompare(params);
  if (path === "/experiment") return viewExperiment(params);
  if (path === "/leaderboard") return viewLeaderboard(params);
  if (path === "/cards") return viewCards(params);
  if (path === "/card") return viewCard(params);
  if (path === "/models") return viewModels(params);
  if (path === "/new") return viewNew();
  if (path === "/about") return viewAbout(params);
  $view.innerHTML = stateHtml("nomatch", {
    heading: true, title: "No such view",
    body: `There is nothing at <code>${esc(path)}</code>.`,
    action: { label: "Go to Now", href: "#/" } });
}

function renderUnavailable() {
  $view.innerHTML = stateHtml("missing", {
    heading: true, title: "Not available on this read-only build", attrs: 'id="not-available"',
    body: `This page launches or changes runs, which only the local observatory can do.
           Run <code>python harness.py serve</code> on the machine that holds the runs.`,
    action: { label: "Go to Now", href: "#/" } });
}

function renderError(e) {
  const signIn = isHosted() && e.network
    ? `<button type="button" class="btn" data-act="signin">Sign in again</button>` : "";
  const meaning = e.status === 404
    ? "Nothing is wrong with the page; the item may be outside this data set."
    : e.network ? "The request did not reach the server, so nothing here is out of date or lost."
    : "The request reached the server but did not succeed.";
  $view.innerHTML = stateHtml("error", {
    heading: true, title: "This view could not load", role: "alert",
    body: `${esc(e.message)}<br><span class="dim">${esc(meaning)}</span>`,
    action: { label: "Retry", id: "retry" } }).replace("</div>", `${signIn ? `<p class="state-act">${signIn}</p>` : ""}</div>`);
  document.getElementById("retry").addEventListener("click", () => route());
}

/* The tab title follows the view's one h1. */
function titleFromView() {
  const h = $view.querySelector("h1");
  document.title = h ? `${h.textContent.trim()} · orchestral` : "orchestral · observatory";
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
      titleFromView();
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
