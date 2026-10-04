/* Artifact viewer: a toolbar (viewport 375 / 768 / 1280, open raw, copy path) over a
   framed preview on a sunken stage. HTML runs in a sandboxed iframe; images get alt
   text from the task title; text and unknown types show in an inert frame; a zip
   lists its members and previews one at a time. */
import { esc } from "../util.js";
import { copyText } from "./json-viewer.js";

const VIEWPORTS = [["375", "Phone 375"], ["768", "Tablet 768"], ["1280", "Desktop 1280"]];
const IMAGES = ["png", "jpg", "jpeg", "svg", "webp", "gif"];

function fmtBytes(n) {
  if (n == null) return "";
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

const extOf = name => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");

function frameHtml(src, ext, title) {
  if (IMAGES.includes(ext)) return `<img class="artifact-img" src="${esc(src)}" alt="${esc(title)}">`;
  if (ext === "html") return `<iframe class="artifact-frame" title="${esc(title)}" sandbox="allow-scripts" src="${esc(src)}"></iframe>`;
  return `<iframe class="artifact-frame av-text" title="${esc(title)}" sandbox="" src="${esc(src)}"></iframe>`;
}

export function artifactHtml(runId, a, { title, previewMembers = true } = {}) {
  const raw = `/api/run/${encodeURIComponent(runId)}/artifact`;
  const isZip = a.ext === "zip";
  const toolbar = `<div class="av-bar">
    <div class="av-viewports" role="group" aria-label="Preview width">${VIEWPORTS.map(([w, label]) =>
      `<button type="button" data-w="${w}" aria-pressed="false">${label}</button>`).join("")}
      <button type="button" data-w="fit" aria-pressed="true">Fit</button></div>
    <a class="btn" href="${raw}" target="_blank" rel="noopener">Open raw</a>
    <button type="button" class="av-copy" data-path="${esc(a.path || a.name)}">Copy path</button>
  </div>`;
  const meta = `<div class="artifact-meta"><span>${esc(a.name)}</span><span>${esc(fmtBytes(a.bytes))}</span></div>`;
  if (isZip) {
    const members = a.members || [];
    return `${meta}${toolbar}
      <div class="member-list" role="group" aria-label="Archive members">${members.map(m =>
        previewMembers
          ? `<button type="button" data-m="${esc(m.name)}">${esc(m.name)} <span class="dim">${esc(fmtBytes(m.bytes))}</span></button>`
          : `<span class="chip chip-dim">${esc(m.name)} <span class="dim">${esc(fmtBytes(m.bytes))}</span></span>`).join("")}</div>
      <div class="av-stage" id="member-view">${previewMembers ? `<div class="empty">Select a file to preview</div>` : ""}</div>`;
  }
  return `${meta}${toolbar}<div class="av-stage">${frameHtml(raw, a.ext, title)}</div>`;
}

export function bindArtifact(root, runId, a, { title, previewMembers = true } = {}) {
  const stage = root.querySelector(".av-stage");
  const bar = root.querySelector(".av-bar");
  if (!stage || !bar) return;
  bar.querySelector(".av-viewports").addEventListener("click", e => {
    const b = e.target.closest("button[data-w]");
    if (!b) return;
    for (const x of bar.querySelectorAll(".av-viewports button")) x.setAttribute("aria-pressed", String(x === b));
    stage.style.setProperty("--av-w", b.dataset.w === "fit" ? "100%" : `${b.dataset.w}px`);
    stage.dataset.width = b.dataset.w;
  });
  bar.querySelector(".av-copy").addEventListener("click", e => copyText(e.currentTarget.dataset.path, e.currentTarget));
  if (a.ext !== "zip" || !previewMembers) return;
  const btns = [...root.querySelectorAll(".member-list button")];
  for (const b of btns) {
    b.addEventListener("click", () => {
      for (const x of btns) x.classList.toggle("active", x === b);
      const name = b.dataset.m;
      stage.innerHTML = frameHtml(`/api/run/${encodeURIComponent(runId)}/artifact/${encodeURIComponent(name)}`, extOf(name), `${title}: ${name}`);
    });
  }
  btns[0]?.click();
}
