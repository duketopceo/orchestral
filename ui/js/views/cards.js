import { $view } from "../dom.js";
import { data } from "../data.js";
import { setFlags, bindFlags, flagWidget } from "../flags.js";
import { esc, slug } from "../util.js";

function galleryCardHref(card, lens, group) {
  const query = new URLSearchParams({ kind: card.kind, target: card.target, lens });
  if (group) query.set("group", group);
  return `#/card?${query.toString()}`;
}

function galleryCard(card, lens, group) {
  const story = card.story || {};
  const cohort = story.cohort || {};
  const href = galleryCardHref(card, lens, group);
  const proof = story.proof || {};
  const title = card.kind === "group"
    ? (card.group_label || card.target)
    : card.kind === "pairing"
      ? `${slug(card.orchestrator)} → ${slug(card.worker)}`
      : (card.task_title || card.task_id || card.target);
  const context = card.kind === "group"
    ? `${cohort.runs || card.runs || 0} runs · ${cohort.tasks || card.tasks || 0} tasks · ${cohort.pairings || 0} pairings`
    : card.kind === "pairing"
      ? `${card.finished || 0}/${card.runs || 0} finished · ${card.tasks || 0} tasks`
      : `${card.task_id || "run"} · ${card.run_group || "ungrouped"}`;
  const metrics = (story.metrics || []).slice(0, 3);
  const flags = card.flag
    ? `<span class="chip ${card.flag === "interesting" ? "chip-acc" : "chip-fail"}">${card.flag === "not" ? "Not interesting" : "Flagged"}</span>`
    : "";
  const proofLabel = proof.status === "available" ? "Proof ready" : proof.status === "partial" ? "Partial proof" : "No stored proof";
  return `<article class="gallery-card panel${card.flag === "interesting" ? " story-selected" : ""}">
    <div class="gallery-card-top"><span class="chip chip-dim">${esc(card.kind[0].toUpperCase() + card.kind.slice(1))}</span><span class="gallery-card-flags">${flags}${flagWidget(card.kind, card.target)}</span></div>
    <a class="gallery-card-title" href="${href}">${esc(title)}</a>
    <div class="gallery-card-context">${esc(context)}</div>
    <p class="gallery-card-claim">${esc(story.claim || card.verdict_line || "Evidence is still incomplete.")}</p>
    <div class="gallery-card-metrics">${metrics.map(metric => `<span><b>${esc(metric.value)}</b><small>${esc(metric.label)}</small></span>`).join("")}</div>
    <div class="gallery-card-foot"><span class="proof-status ${proof.status || "unavailable"}">${esc(proofLabel)}</span>
      <span class="gallery-card-actions"><a class="btn" href="${href}">Open</a><a class="btn" href="/api/shot.png?route=${encodeURIComponent(href.slice(1))}" download>PNG</a></span>
    </div>
  </article>`;
}

export async function viewCards(params) {
  const groups = await data.groups();
  const requestedGroup = params.get("group");
  const group = requestedGroup !== null
    ? requestedGroup
    : (groups[0] && groups[0].group) || "";
  const scope = params.get("scope") || "group";
  const lens = params.get("lens") || "overall";
  const flagged = params.get("flagged") === "1";
  const [catalog, flags] = await Promise.all([
    data.cards({ group, scope, lens, flagged }), data.flags(),
  ]);
  setFlags(flags);
  const lenses = catalog.lenses || [];
  const activeLens = lenses.find(item => item.id === lens) || lenses[0] || { id: lens, label: lens };
  const cards = catalog.cards || [];
  const cardHash = () => {
    const next = new URLSearchParams({ scope, lens: activeLens.id });
    if (group) next.set("group", group);
    if (flagged) next.set("flagged", "1");
    return `#/cards?${next.toString()}`;
  };
  $view.innerHTML = `
    <h1>Cards</h1>
    <p class="page-sub">Choose a cohort, select a lens, and open a shareable card with its real proof.
    Cards are local exports; nothing is posted automatically.</p>
    <div class="gallery-controls panel">
      <div class="gallery-control-row">
        <label class="f">Run group<select id="cards-group"><option value="">All groups</option>${groups.map(g =>
          `<option value="${esc(g.group)}" ${g.group === group ? "selected" : ""}>${esc(g.label || g.group)} · ${g.runs} runs</option>`).join("")}</select></label>
        <label class="f">Scope<select id="cards-scope">${(catalog.scopes || []).map(item =>
          `<option value="${esc(item.id)}" ${item.id === scope ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="f">Lens<select id="cards-lens">${lenses.map(item =>
          `<option value="${esc(item.id)}" ${item.id === activeLens.id ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="check-line gallery-flag-filter"><input id="cards-flagged" type="checkbox" ${flagged ? "checked" : ""}> Flagged only</label>
      </div>
      <div class="gallery-current"><span class="eyebrow">${esc(activeLens.label)}</span>
        <span>${cards.length} card${cards.length === 1 ? "" : "s"}${group ? ` · ${esc(group)}` : " · All groups"}</span>
        <a class="btn" href="#/leaderboard?${new URLSearchParams({ group, lens: activeLens.id }).toString()}">Open leaderboard →</a></div>
    </div>
    <div class="gallery-grid">${cards.map(card => galleryCard(card, activeLens.id, group)).join("") ||
      `<div class="empty gallery-empty">No cards match this view.<br><a href="${cardHash()}">Clear filters</a></div>`}</div>`;
  for (const [id, key] of [["cards-group", "group"], ["cards-scope", "scope"], ["cards-lens", "lens"]]) {
    document.getElementById(id).addEventListener("change", e => {
      const next = new URLSearchParams({ scope, lens: activeLens.id });
      const value = e.target.value;
      if (key !== "group" && group) next.set("group", group);
      if (value) next.set(key, value);
      if (flagged) next.set("flagged", "1");
      location.hash = `#/cards?${next.toString()}`;
    });
  }
  document.getElementById("cards-flagged").addEventListener("change", e => {
    const next = new URLSearchParams({ scope, lens: activeLens.id });
    if (group) next.set("group", group);
    if (e.target.checked) next.set("flagged", "1");
    location.hash = `#/cards?${next.toString()}`;
  });
  bindFlags($view);
}
