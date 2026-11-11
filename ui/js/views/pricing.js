/* Model Pricing view — input × output × cache-read price per Mtok, with a
   generic↔agent mix slider. Agents re-read their context every turn (~97%
   cache reads), so a cheap-headline model with a high cache-read rate can be
   ~3x more expensive in agent workloads; this view surfaces that live. */

import { $view } from "../dom.js";
import { esc } from "../util.js";

const PRICES = [
  { id: "claude-opus-55", name: "Claude Opus 5.5", in: 3.5, out: 15.5, cache: 0.5 },
  { id: "kimi-k31-pro", name: "Kimi K3.1 Pro", in: 2.5, out: 9.5, cache: 0.2 },
  { id: "grok-47", name: "Grok 4.7", in: 3.0, out: 8.0, cache: 0.5 },
  { id: "glm-53", name: "GLM-5.3", in: 2.7, out: 5.0, cache: 0.1 },
  { id: "gpt-61-sol", name: "GPT-6.1 Sol", in: 1.8, out: 8.5, cache: 0.1 },
  { id: "gpt-61-terra", name: "GPT-6.1 Terra", in: 1.0, out: 4.5, cache: 0.1 },
];

// Workload presets: (weight_in, weight_cache_read, weight_out).
const GENERIC = { wIn: 0.75, wCache: 0.0, wOut: 0.25 };   // 3 input : 1 output
const AGENT = { wIn: 0.025, wCache: 0.97, wOut: 0.005 };  // agents ~97% cache reads

function lerpWeights(v) {
  const { wIn: i0, wCache: c0 } = GENERIC;
  const { wIn: i1, wCache: c1 } = AGENT;
  return { wIn: i0 + (i1 - i0) * v, wCache: c0 + (c1 - c0) * v };
}

function blendedFor(m, v) {
  const { wIn, wCache, wOut } = blendFor(v);
  return wIn * m.in + wCache * m.cache + wOut * m.out;
}

function blendFor(v) {
  const { wIn, wCache } = lerpWeights(v);
  return { wIn, wCache, wOut: 1 - wIn - wCache };
}

function fmtDollar(v) {
  if (v == null) return "—";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v < 0.001) return `$${(v * 1000).toFixed(2)}m`;
  return `$${v.toFixed(3)}`;
}

export async function viewPricing(params = new URLSearchParams()) {
  const mix = Number(localStorage.getItem("orch.pricing.mix") ?? 1);

  $view.innerHTML = `
    <h1>Model Pricing</h1>
    <p class="page-sub">Input × output × cache-read price per Mtok. Drag the mix to
    see why a cheap headline model is an agent-cost bomb: agents re-read their context
    every turn, so ~97% of tokens are cheap cache reads.</p>

    <div class="panel panel-pad">
      <div class="split" style="align-items:center">
        <label class="f" style="flex:3;margin:0">
          <span>Blend — <b><code id="px-mix-label"></code></b></span>
          <input type="range" id="px-mix" min="0" max="1" step="0.01" value="${mix}" style="width:100%">
        </label>
        <div class="dim" style="text-align:right">cache reads <b id="px-cache-pct"></b>%</div>
      </div>
      <p class="dim sm" id="px-weight-note"></p>
    </div>

    <div class="panel panel-pad" id="px-3d"><div class="loading">Plotting…</div></div>

    <h2>Blended cost @ current mix</h2>
    <p class="page-sub">Cheapest first. Markers on the 3D plot are colored by the same blend.</p>
    <div class="panel"><table class="data" id="px-table"><tr>
      <th>Model</th><th class="t-num">Input</th><th class="t-num">Cache read</th>
      <th class="t-num">Output</th><th class="t-num">Blended $/Mtok</th><th class="t-num">vs cheapest</th>
    </tr><tbody id="px-tbody"></tbody></table></div>
    <p class="dim sm">List API prices, Nov 6 2026, prompts &lt; 200K tokens. Units: USD per million tokens.</p>`;

  const $slider = document.getElementById("px-mix");
  const $label = document.getElementById("px-mix-label");
  const $cachePct = document.getElementById("px-cache-pct");
  const $weightNote = document.getElementById("px-weight-note");
  const $tbody = document.getElementById("px-tbody");
  const $three = document.getElementById("px-3d");

  function mixLabel(v) {
    if (v <= 0.005) return "Generic · 3:1 in:out (no cache)";
    if (v >= 0.995) return "Agent · 97% cache reads";
    const { wCache } = lerpWeights(v);
    return `Agent ${Math.round(wCache * 100)}% cache`;
  }

  function renderTable(v) {
    const rows = PRICES.map(m => ({ ...m, blend: blendedFor(m, v) }))
      .sort((a, b) => a.blend - b.blend);
    const best = rows[0].blend;
    $tbody.innerHTML = rows.map((m, i) => `
      <tr${i === 0 ? ' class="story-selected"' : ""}>
        <td>${esc(m.name)}${i === 0 ? ' <span class="chip chip-acc">cheapest</span>' : ""}</td>
        <td class="t-num">${fmtDollar(m.in)}</td>
        <td class="t-num">${fmtDollar(m.cache)}</td>
        <td class="t-num">${fmtDollar(m.out)}</td>
        <td class="t-num">${fmtDollar(m.blend)}</td>
        <td class="t-num">${i === 0 ? "—" : fmtDollar(m.blend - best)}</td>
      </tr>`).join("");
  }

  function renderPlot(v) {
    // plotly.js is loaded `defer` in app.html; guard live (retry loop below).
    if (typeof window.Plotly === "undefined") return;
    const rows = PRICES.map(m => ({ ...m, blend: blendedFor(m, v) }));
    const data = [{
      type: "scatter3d",
      mode: "markers+text",
      x: rows.map(m => m.in),
      y: rows.map(m => m.out),
      z: rows.map(m => m.cache),
      text: rows.map(m => m.name),
      textposition: "top center",
      hovertemplate: "<b>%{text}</b><br>Input %{x:.2f}<br>Output %{y:.2f}<br>Cache-read %{z:.2f}<br>Blended $%{customdata:.3f}/Mtok<extra></extra>",
      customdata: rows.map(m => m.blend),
      marker: {
        size: 12,
        color: rows.map(m => m.blend),
        colorscale: "Viridis",
        showscale: true,
        colorbar: { title: "$/Mtok @ mix", thickness: 12, outlinewidth: 0 },
      },
    }];
    const layout = {
      height: 420,
      margin: { l: 10, r: 10, t: 10, b: 10 },
      paper_bgcolor: "rgba(0,0,0,0)",
      font: { color: "#cbd5e1" },
      scene: {
        xaxis: { title: "Input $/Mtok", nticks: 5 },
        yaxis: { title: "Output $/Mtok", nticks: 5 },
        zaxis: { title: "Cache-read $/Mtok", nticks: 5 },
        bgcolor: "rgba(0,0,0,0)",
      },
    };
    window.Plotly.newPlot($three, data, layout, { displayModeBar: false, responsive: true });
  }

  function draw(v) {
    localStorage.setItem("orch.pricing.mix", String(v));
    $label.textContent = mixLabel(v);
    const { wIn, wCache } = lerpWeights(v);
    $cachePct.textContent = Math.round(wCache * 1000) / 10;
    $weightNote.textContent =
      `input ${(100 * wIn).toFixed(1)}% · cache-read ${(100 * wCache).toFixed(1)}% · output ${(100 * (1 - wIn - wCache)).toFixed(1)}% of a priors-weighted Mtok`;
    renderTable(v);
    renderPlot(v);
  }

  $slider.addEventListener("input", () => draw(Number($slider.value)));
  $slider.addEventListener("change", () => draw(Number($slider.value)));
  draw(mix);

  // plotly.js is deferred; paint the 3D plot once it lands, or fall back.
  const deadline = Date.now() + 10000;
  const iv = setInterval(() => {
    if (typeof window.Plotly !== "undefined") {
      clearInterval(iv);
      renderPlot(Number($slider.value));
    } else if (Date.now() > deadline) {
      clearInterval(iv);
      $three.innerHTML = '<div class="empty">Plotly failed to load over the CDN — the table above still works.</div>';
    }
  }, 150);
}
