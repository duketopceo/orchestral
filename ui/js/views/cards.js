import { $view } from "../dom.js";
import { data } from "../data.js";
import { setFlags } from "../flags.js";
import { esc, fmtPct, slug } from "../util.js";
import { stateHtml } from "../components/states.js";
import { bindFit, cardModel, fitFrame, programCard, waitForCardAssets } from "../components/program-card.js";

function cardHref(card, lens, group) {
  const query = new URLSearchParams({ kind: card.kind, target: card.target, lens });
  if (group) query.set("group", group);
  return `#/card?${query.toString()}`;
}

function rowTitle(card) {
  return card.kind === "group"
    ? (card.group_label || card.target)
    : card.kind === "pairing"
      ? `${slug(card.orchestrator)} / ${slug(card.worker)}`
      : (card.task_title || card.task_id || card.target);
}

function rowContext(card, cohort) {
  return card.kind === "group"
    ? `${cohort.runs || card.runs || 0} runs, ${cohort.tasks || card.tasks || 0} tasks, ${cohort.pairings || 0} pairings`
    : card.kind === "pairing"
      ? `${card.finished || 0} of ${card.runs || 0} finished, ${card.tasks || 0} tasks`
      : `${card.task_id || "run"}, ${card.run_group || "ungrouped"}`;
}

/* One row of the publish list: a link, so the whole row is the target. The
   selected row is the one whose card is previewed beside the list. */
function publishRow(card, index, selected, rowHref) {
  const story = card.story || {};
  const rate = card.kind === "run" ? "" : `<span class="pub-row-num">${card.pass_rate == null ? "" : fmtPct(card.pass_rate)}</span>`;
  const low = story.confidence?.mechanical?.level === "low" && card.kind !== "run";
  return `<a class="pub-row" href="${rowHref(index)}"${selected ? ' aria-current="true"' : ""}>
    <span class="pub-row-kind">${esc(card.kind === "group" ? "Run group" : card.kind === "pairing" ? "Pairing" : "Run")}</span>
    <span class="pub-row-title">${esc(rowTitle(card))}</span>
    ${rate}
    <span class="pub-row-ctx">${esc(rowContext(card, story.cohort || {}))}${low ? " (thin sample)" : ""}</span>
  </a>`;
}

export async function viewCards(params) {
  const groups = await data.publishGroups();
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
  const selIndex = Math.min(Math.max(parseInt(params.get("sel") || "0", 10) || 0, 0), Math.max(cards.length - 1, 0));
  const cardHash = () => {
    const next = new URLSearchParams({ scope, lens: activeLens.id });
    if (group) next.set("group", group);
    if (flagged) next.set("flagged", "1");
    return `#/cards?${next.toString()}`;
  };
  const rowHref = index => `${cardHash()}&sel=${index}`;
  const picked = cards[selIndex];
  const preview = picked
    ? `<div id="pub-preview" class="pub-preview">
        ${fitFrame(programCard(cardModel(picked, picked.kind, activeLens.id, picked.story?.proof)), { max: 960 })}
        <div class="pub-preview-foot"><a class="btn primary" href="${cardHref(picked, activeLens.id, group)}">Open in the editor</a>
          <span class="hint">${esc(rowTitle(picked))}</span></div>
      </div>`
    : "";
  $view.innerHTML = `
    <h1>Publish</h1>
    <p class="page-sub">Choose a cohort and a lens, preview the card, then open it to write the alt text and make the PNG.
    Nothing is posted automatically.</p>
    <div class="gallery-controls panel">
      <div class="gallery-control-row">
        <label class="f">Run group<select id="cards-group"><option value="">All groups</option>${groups.map(g =>
          `<option value="${esc(g.group)}" ${g.group === group ? "selected" : ""}>${esc(g.label || g.group)}, ${g.runs} runs</option>`).join("")}</select></label>
        <label class="f">Scope<select id="cards-scope">${(catalog.scopes || []).map(item =>
          `<option value="${esc(item.id)}" ${item.id === scope ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="f">Lens<select id="cards-lens">${lenses.map(item =>
          `<option value="${esc(item.id)}" ${item.id === activeLens.id ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="check-line gallery-flag-filter"><input id="cards-flagged" type="checkbox" ${flagged ? "checked" : ""}> Flagged only</label>
      </div>
      <div class="gallery-current"><span class="eyebrow">${esc(activeLens.label)}</span>
        <span>${cards.length} card${cards.length === 1 ? "" : "s"}${group ? `, ${esc(group)}` : ", all groups"}</span>
        <a class="btn" href="#/leaderboard?${new URLSearchParams({ group, lens: activeLens.id }).toString()}">Open pairings</a></div>
    </div>
    ${cards.length ? `<div class="pub-split">
      <nav id="pub-list" class="pub-list" aria-label="Cards to publish">${cards.map((card, i) => publishRow(card, i, i === selIndex, rowHref)).join("")}</nav>
      ${preview}
    </div>` : stateHtml("nomatch", { title: "No cards match this view", action: { label: "Clear filters", href: cardHash() } })}`;
  bindFit($view);
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
  await waitForCardAssets($view);
}
