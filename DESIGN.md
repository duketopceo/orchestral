# orchestral design system: "The Score"

Status: research and specification only. Nothing in this file is implemented yet.
Audited against `origin/main` @ `15b482e` (merge of #118). The checked-out branch
`docs/cloudflare-observatory-plan` has no code delta from `origin/main`; its plan
(`docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md`) is read
here as the near-future context: the same SPA will be served from a Cloudflare
Worker at `obs.shippedit.dev` behind Access.

Method: research-first and audit-first (refero-design + design-taste-frontend
methodology). Live Refero MCP was not available in this session, so the visual
research used the bundled craft references, the local `awesome-design-md`
corpus, category products on the web, and a free local run of the observatory
against a copy of `runs/` with no API keys in the environment (no model calls
were possible, none were made).

Constraint carried from `AGENTS.md`: the stack stays Python 3.11+ with `pyyaml`,
`httpx`, `rich` and the optional extras. Everything below is vanilla CSS, vanilla
JS, hand-authored SVG and self-hosted woff2. No npm build, no new Python deps.

---

## 1. Product read

**What it is.** An evaluation harness that runs the same task through an
*orchestrator* model (plans, delegates, assembles) and a *worker* model
(executes), then grades the result on two independent axes: **mechanical**
(deterministic pass/fail from tests, SQL diffs, file-state checks) and
**judge** (an LLM's 0-1 quality score, advisory unless calibrated). It meters
every call, so the third axis is **cost**. The question it answers: which
pairing gets acceptable work at the lowest cost per pass, and is the answer
statistically real?

**Users.**

| User | Context | What they need from the UI |
|---|---|---|
| Operator (primary, today: one person) | Runs grids/experiments from a laptop, watches live runs, audits failures, decides what to publish | Dense, fast, keyboard-driven; truth about sample size and spend; one-click path from an aggregate number to the raw evidence |
| Reader of a published card (X, README, blog) | Sees a 1200x675 PNG in a feed for ~2 seconds | One claim, one number, the caveat, and a reason to trust it |
| Reviewer of the hosted observatory (Access-gated, soon) | Opens a link from a phone or a different machine | Read-only, legible at 390px, no launch controls |
| Terminal user (TUI / CLI) | SSH, tmux, CI logs | The same vocabulary and verdict glyphs as the web, without color dependence |

**Design read (one line).** Reading this as: a scientific instrument and
publication system for technical evaluators, with an engraved, data-first,
color-means-evidence language, leaning toward a Carbon-style flat-square system
with a custom musical-score identity.

**Dials** (design-taste-frontend): `DESIGN_VARIANCE 4` (product UI, not a
landing page), `MOTION_INTENSITY 3` (motion only to explain state), `VISUAL_DENSITY 8`
for observatory screens, `4` for cards/OG/README.

---

## 2. Current-state audit

### 2.1 Surfaces and stack

| Surface | Files | Stack | Visual language today |
|---|---|---|---|
| Web observatory (`harness.py serve`) | `ui/app.html` (40 lines), `ui/app.css` (886), `ui/app.js` (1318), `orchestral/web/server.py`, `orchestral/web/state.py` (2290), `orchestral/web/catalog.py` | stdlib `http.server`, hash-routed SPA, string-template HTML, no build | Dark "instrument workbench": `#0b0d10` canvas, Tailwind cyan-400 `#22d3ee` accent, Tailwind emerald/red/amber/blue-400 semantics, system-ui + platform mono |
| Error pages | `orchestral/web/render.py` | inline `<style>` | Same dark palette, hard-coded hex, title uses em dash |
| Share cards (X-ready PNG) | `.xcard` in `ui/app.css` + `viewCard()` in `ui/app.js`; captured by `orchestral/shots.py` via `/api/shot.png` | Playwright screenshot of the live SPA | Dark gradient card; two grammars coexist (older exported cards in `reports/cards/*.png` use a different hero/table layout than the current "universal card") |
| Static reports (`report --html`, `dashboard`, gallery) | `orchestral/reporter.py` | f-string HTML with inline `<style>` | **Light** Tailwind-gray theme, `#2563eb` blue links, Tailwind 8-color scatter palette (`_SCATTER_PALETTE`), dark `#1f2937` code blocks |
| TUI (`harness.py tui`) | `orchestral/tui/app.py`, `screens.py`, `widgets.py`, `state.py` | Textual | Textual default theme (`$primary`, `$success`...), no brand |
| CLI tables (`report`, `history`, `coverage`) | `harness.py` | `print` with fixed widths | Plain ASCII; columns break when slugs exceed width; raw ms values (`4008538`) |
| README / social | `README.md`, `docs/assets/social.png` | static PNG | Olive-on-charcoal mono card with a treble-clef **emoji** as the logo |

**Finding 0: there are five unrelated visual languages** (dark cyan SPA, light
Tailwind reports, olive social card, default Textual, raw ASCII). Nothing shares
tokens. This is the root problem; everything else is a symptom.

### 2.2 Tokens today (`ui/app.css :root`)

- One radius (`6px`) but chips are `99px` pills, cards `12-18px`: no documented
  radius rule.
- `--text-3: #57636f` on `--bg #0b0d10` is **3.17:1**, on `--bg-raised` **2.98:1**.
  It is used for every table header, section label, timestamp, metadata line and
  hint: the most-read small text in the product fails WCAG AA.
- Accent `#22d3ee` is Tailwind cyan-400 verbatim; semantics are Tailwind
  emerald-400 / red-400 / amber-400 / blue-400 verbatim. Recognisably default.
- Fonts: `system-ui` and a platform mono fallback list, so the product looks
  different on every OS and has no typographic identity.
- Shadows: none (good). Hairlines: yes (good, keep).

### 2.3 Screen-by-screen findings (from code + local screenshots at 1440 and 390)

**Shell / rail** (`ui/app.html`)
- Brand is a `◆` glyph + the word "orchestral" + "observatory". No logo exists.
- No favicon: `GET /favicon.ico` returns **404**. No `<meta name="theme-color">`,
  no OG tags, title uses `·`.
- At <=860px the rail collapses to the *first letter* of each nav label
  (`#nav a::first-letter`): "O R L C M C A +". Two "C"s (Compare, Cards) are
  indistinguishable; there are no icons to fall back on.
- Rail "Activity" polls `/api/overview` every 5s forever, on every screen, in
  addition to each view's own fetches.

**Overview** (`viewOverview`)
- Page height ~4,900px at 1440 wide. A 64-row experiment ledger, ~55 of them
  `pending` placeholder rows, sits above the failure taxonomy and the heatmap. The
  thing the operator came for (what changed, what is running, what needs a look)
  is not above the fold.
- Group cards title themselves with raw keys such as
  `jev-ab:landing-page-coffee:z-ai/glm-5.3-flash:z-ai/glm-5.3-flash:jev`, wrapped
  over four lines in cyan mono. The `label` field exists but most groups lack it.
- Every card and row carries a `★ ∅` flag pair: ~150 identical micro-buttons per
  page, unlabeled for screen readers beyond `title`.
- The group progress bar's fail-segment width expression is algebraically
  muddled (`fail * 100 * (g.finished ? 1 : 0) / Math.max(g.finished, 1) * ...`)
  and renders wrong proportions.
- Chip rows use class `.m`, which is not defined in CSS: chips touch with no gap.
- Tasks x Pairings heatmap: the `th.heat-task` inherits the uppercase table-header
  style, so task **titles render in ALL CAPS** ("FIZZBUZZ MODULE"). Low-n cells are
  painted **amber** (`--warn`) at full strength, so a 1-sample 100% reads as a
  warning while the 3-sample cells are green: sample size and outcome share one
  channel. The table overflows horizontally with no sticky first column. Cells are
  clickable `<td>`s with no keyboard access. Copy says "a dash means never
  attempted" but renders `·`.
- Failure taxonomy bars are all the same red at reduced opacity; no link to the
  runs behind each bar.

**Runs** (`viewRuns`)
- Unpaginated: 10,800px page. Filters fire a request on every keystroke with no
  debounce. Two search boxes ("Task, model, or reason" and "Task ID") overlap in
  purpose. Placeholder-as-label throughout.
- Group column shows the raw multi-colon key.

**Run detail** (`viewRun`)
- The header splits into a left identity block and a right stat cluster; at
  1440 the stats float top-right far from the title, at 390 they wrap into a
  ragged two-row cluster.
- Phase "evidence chain" strip renders as an empty 2px line when the timeline is
  empty; Events tab renders an empty bordered box with no message. Empty states
  are undesigned.
- Tabs are `<button>`s without `role="tab"`/`aria-selected`; events rows are
  click-only `div`s.
- Artifact preview is a white iframe in a dark UI with no frame/device chrome or
  viewport toggle; image artifacts have no `alt`.
- Report / Plan / Manifest are raw `JSON.stringify` dumps.

**Leaderboard** (`viewLeaderboard`)
- Defaults to the *first* run group, which on this data set is a single-pairing
  cohort: all five story lenses select the same row, the matrix is 1x1, and the
  scatter says "Need at least 2 metered pairings". The default view is the least
  informative one available.
- Scatter (`lbScatter`): labels collide and clip at the top edge, no x-axis tick
  values (log scale with no numbers), the `<svg>` scales with container width so
  9px labels render at ~14px at 1440. Dots are click-only.
- Matrix uses two unrelated encodings (cyan fill for normal, gray fill + dashed
  border for low-n) and a separate cyan from the heatmap's green: the same
  measure (mechanical pass rate) is drawn in three colors across two screens.
- Ranking table: the rank cell "1 / 25" wraps; CI "87-100%" wraps; 13 columns
  with no column priority on narrow widths.

**Cards gallery and card** (`viewCards`, `viewCard`)
- The `.xcard` is a fixed 1200px box inside a ~1180px content column: at a
  1440 viewport the card's **right edge is clipped**.
- When proof is missing (common), two large "Unavailable" panels consume ~45% of
  the card, the most expensive pixels in a social image spent saying "nothing".
- The toolbar pre-fills `moonshotai/kimi-k2` as the thread writer, so **one click
  on "Write thread" spends money** on a paid model with no estimate and no confirm.
- Card brand is the same `◆ orchestral observatory` text lockup; the exported PNGs
  in `reports/cards/` use an older layout (hero numbers + task table) so published
  cards do not look like a series.

**New run** (`viewNew`)
- "Dry run" is unchecked by default and there is no cost estimate before launch,
  although `experiment --dry-run` already knows how to price a plan. A paid
  launch is one click from a default form.
- Labels sit above inputs (good); the task picker is a bare `<select>` of ~150
  ids with no search, type, or difficulty.

**Models, Compare, About**: serviceable tables. Compare has no visualization of
deltas (a column of `+12pp` text). About is the best-written page in the product
and should become inline help, not a destination.

**Static reports** (`reporter.py`): a separate light theme with Tailwind
colors, a scatter that plots unjudged runs as 1.0/0.0 on the judge axis (mixes
axes, the exact thing the SPA warns against), and an 8-color categorical palette
that is not colorblind-safe.

**TUI**: functional and well-structured (Live / History / Leaderboard / Detail,
`?` help), but entirely Textual defaults; status text and color are mapped in
`tui/state.py` (`"text carries the meaning, color only decorates"`, a good rule
to keep and extend).

**CLI tables**: fixed-width columns overflow on long slugs, latency printed as
raw ms, no low-n marker, no glyphs.

### 2.4 Copy audit

- Em dash used as the universal null (`fmtMoney/fmtPct/fmtScore` return `"—"`)
  and as a separator in page subtitles, titles and error pages.
- `·` used as the default separator everywhere (metadata strips carry 4-6 of
  them).
- Uppercase letter-spaced micro labels above nearly every section (`h2` style),
  which is the "eyebrow on every section" pattern.
- Good: the product already speaks precisely ("Advisory semantic axis",
  "uncalibrated (0 pairs)", "Low n"). The voice is the asset; keep it.

### 2.5 UX flows and friction

1. **"What happened overnight?"** Overview -> scroll past groups and 60 ledger rows
   -> heatmap -> click a cell -> Runs filtered by task only (pairing filter is lost)
   -> open a run. 5+ steps, filter context dropped.
2. **"Why did this fail?"** Run detail -> Events (could be empty) -> Report (raw
   JSON). There is no failure summary at the top of a failed run.
3. **"Is A better than B?"** Compare requires knowing group keys; result is a
   table of text deltas with no CI.
4. **"Publish this."** Leaderboard -> lens -> Create card -> card clipped at
   1440 -> Download PNG (headless capture) -> optional paid thread. No preview of
   what the PNG will look like at feed size, no alt text produced.
5. **"Launch."** New run defaults to a paid run with no estimate.

---

## 3. Asset inventory

### 3.1 Existing assets

| Asset | Path | Verdict | Reason |
|---|---|---|---|
| Social preview 1280x640 | `docs/assets/social.png` | **Redo** | Treble-clef emoji as logo; olive palette unrelated to the product; describes the product as "Multi-agent task orchestration: DAGs" (it is an eval harness); fake status "4 done · 1 running" |
| Brand mark | `◆` text glyph in `ui/app.html`, `.xcard .xc-brand` | **Redo** | Not a logo; renders differently per font |
| Favicon / app icons | none | **Missing** | 404 on `/favicon.ico` |
| Fonts | none (system stacks) | **Missing** | No identity, cross-OS drift |
| Icons | Unicode only: `◆ ★ ∅ ↗ → ▸ ⚠ ✓ ✕` | **Redo** | Inconsistent metrics, ambiguous meaning (`∅` = "not interesting"), not keyboard/AT friendly |
| Charts: leaderboard scatter | `lbScatter()` in `ui/app.js` | **Redo** | Collisions, no ticks, scale drift, click-only |
| Charts: heatmaps (overview, matrix) | `viewOverview`, `viewLeaderboard` | **Redo** | Two encodings for one measure; low-n conflated with warn |
| Charts: reporter scatter + bars | `orchestral/reporter.py` | **Redo** | Tailwind palette, mixed axes |
| Share card layout | `.xcard*` in `ui/app.css` | **Redo** | Clipped at 1440, empty-proof waste, layout drift vs exported PNGs |
| Exported cards | `reports/cards/*.png` (local, untracked) | **Regenerate** | Old grammar |
| Empty / loading / error states | `.empty`, `.loading`, `render.py` | **Redo** | Plain gray text, no guidance, no illustration slot |
| OG/Twitter meta | none | **Missing** | Hosted observatory links unfurl as bare URLs |
| Motion | `@keyframes pulse` only | **Redo** | Infinite opacity pulse on every running dot; no reduced-motion guard |
| TUI theme | Textual default | **Missing** | No brand theme |

### 3.2 New custom assets (all made in-house, no stock, no icon packs)

Each asset has a creative brief, a production method, and an owner file.

**A1. Logomark: "Downbeat"**
- Brief: the orchestrator is a conductor, workers are the players. The mark is a
  musical staff reduced to its engineering essence: **three horizontal staff
  lines** (workers) crossed by **one rising diagonal stroke** that ends just above
  the top line (the conductor's baton on the downbeat, and also a "plan -> delegate"
  vector). No clef, no notes, no emoji. Reads as a tally/measurement mark at 16px
  and as a score fragment at 512px.
- Geometry: 24-unit grid; lines at y=8/12/16, 1.5u stroke, square caps; baton from
  (5,19) to (19,4), 2u stroke, square caps; the baton breaks the middle line with a
  1u gap on each side (knockout) so the mark stays legible in one color.
- Colors: one color only (`--ink`). Never on a gradient, never in a rounded tile.
- Production: hand-authored SVG in `ui/brand/mark.svg`; optical variants
  `mark-16.svg` (2 lines + baton, 2px strokes snapped to pixel grid) and
  `mark-24.svg`. Verify at 16/24/32/512 in both themes.

**A2. Wordmark**
- Brief: `orchestral` set in lowercase Instrument Sans SemiBold, `wdth 90`,
  tracking -1%, with the baton stroke of A1 replacing the *crossbar region of the
  `t`*: a single custom glyph so the wordmark is ownable without being a gimmick.
- Production: outline the text in a font editor (FontForge/Glyphs, free path:
  FontForge), redraw the `t`, export `ui/brand/wordmark.svg` with `<title>`. Lockups:
  horizontal (mark + wordmark, gap = 0.5 x cap height) and stacked.
- "observatory" is dropped from the lockup. Surface names live in the UI, not the
  logo.

**A3. Favicon and app icons**
- `ui/brand/favicon.svg` (A1 16px variant, with `prefers-color-scheme` media query
  inside the SVG to swap ink), `favicon.ico` (16/32/48, built with ImageMagick
  from the SVG), `apple-touch-icon.png` 180x180 (mark on `--paper` square, no
  rounding baked in), `icon-512.png`, `site.webmanifest`. Served by
  `orchestral/web/server.py` static route and by the Worker `[assets]`.

**A4. Icon set: "Engraved 16"** (~32 glyphs)
- Brief: drawn for this product on a 16px grid, 1.5px stroke, square caps and
  joins, 2px corner radius max, no fills except state glyphs. Optically matched to
  Instrument Sans at 13px. Musical-notation influence only where it carries
  meaning (see list); everything else is plain instrument iconography.
- Required glyphs: overview, runs, leaderboard, compare, cards, models, about/help,
  new run, live (filled 6px dot inside 12px ring), pass (filled square with check
  knockout), fail (hollow square with diagonal), cancelled (hollow square, dashed),
  inconclusive (half-filled square), not-judged (hollow circle), judge (semicircle
  gauge), cost (stacked coins as three short bars), latency (stopwatch), tokens
  (two stacked chevrons), low-n (3 diagonal hatch lines in a square, same hatch as
  charts), flag-interesting (fermata: arc over a dot, meaning "hold here"),
  flag-dismiss (rest symbol simplified to a short horizontal block), artifact,
  transcript, plan (staff with 3 ticks), manifest/hash, external link, download,
  copy, filter, search, sort, chevron, close, keyboard, theme toggle.
- Verdict glyphs encode outcome by **shape and fill**, so they survive grayscale,
  CVD, the TUI and the CLI.
- Production: SVG sprite `ui/icons.svg` (`<symbol id="i-pass">`), used as
  `<svg class="i"><use href="#i-pass"/></svg>`, `currentColor` stroke, `aria-hidden`
  with adjacent text. TUI/CLI equivalents defined in section 6.8.

**A5. OG / social images**
- `docs/assets/social.png` 1280x640 (GitHub) and `og-default.png` 1200x630
  (hosted observatory).
- Brief: not a fake UI. A typographic composition: wordmark top-left; the one-line
  claim "Which orchestrator and worker pairing does the job at the lowest cost per
  pass?" in Display L; underneath, a **real** strip-plot of pass rate vs cost per
  pass rendered from scrubbed `runs-pub/` data with the house chart grammar (section
  6.9), axis labels included. Paper theme. Footer: repository URL in mono.
- Production: an HTML template at `ui/og/social.html` using the same CSS tokens,
  captured with the existing `orchestral/shots.py` Playwright path at 2x, then
  `pngquant` (optional) to <=150KB. Regenerated by a `harness.py` subcommand later
  (out of scope here), so the image is always true.

**A6. Share cards (1200x675, X-ready) — "Program note" series**
- Brief: each card reads like a concert program note: scope line, one claim, the
  primary number, its uncertainty drawn as an interval, the caveat, provenance.
  Same grid, type and hatch rules as the app so every card is visibly part of one
  series.
- Layout (paper theme default, stage theme optional):
  - 64px outer margin, 12-column grid, 24px gutters.
  - Row 1: mark + wordmark (left), scope chip "Pairing" / "Run group" / "Run" and
    suite id (right).
  - Row 2: title (Display M, 2 lines max, pairing set as `orchestrator  /  worker`
    with the baton glyph between them).
  - Row 3: **the claim** (Title L, one sentence, generated by `state.py`).
  - Row 4: three measure blocks, no boxes: Mechanical (big number + Wilson interval
    bar), Judge (big number + calibration state word), Cost per pass (big number).
    Blocks separated by 1px vertical rules, not cards.
  - Row 5: proof strip. If proof exists: transcript excerpt (mono) and artifact
    thumbnail side by side. **If no proof exists, row 5 collapses** and the
    measures grow; never render "Unavailable" panels in a published image.
  - Footer: caveat sentence left, provenance (date, n, git short sha) right.
- Production: `.pcard` component in CSS; fixed-size render only inside the capture
  route (`?capture=1`), fluid inside the app so it never clips. Alt text generated
  from the claim + numbers and offered next to Download.

**A7. Empty-state and error illustrations: "Rests"**
- Brief: musical rests are literally "silence, on purpose", the perfect
  metaphor for empty states. Five monoline drawings (64x64, 1.5px stroke, `--ink-3`)
  built from the same staff-line geometry as A1:
  1. No runs yet: an empty three-line staff with a whole-rest block.
  2. Filter matched nothing: staff with a magnifier-shaped fermata.
  3. Run still starting: staff with a single tick and a rising baton.
  4. Evidence missing (no artifact/transcript): staff with a broken middle line.
  5. Server/API error: staff with a double bar line.
- Each state ships with a one-line explanation and one action (a command to copy
  or a link). Production: inline SVG symbols in `ui/icons.svg` (`#r-empty` ...).
  No raster, no stock, no characters.

**A8. Data-viz palette and textures** (spec in 6.9): Okabe-Ito-derived semantic
colors, a single sequential judge ramp, a cost gray ramp, and a 45-degree **hatch
pattern** (`<pattern id="hatch">`, 1px lines at 4px pitch) that always and only
means "low sample / uncertain".

**A9. Motion set** (spec in 6.7): the "baton" live indicator, phase-advance
transition, number tick for live cost/tokens, row-insert highlight for streamed
events. All CSS, all disabled under `prefers-reduced-motion`.

**A10. Typefaces**: Instrument Sans (variable, wdth 75-100, wght 400-700) and IBM
Plex Mono (400/500/600), both SIL OFL 1.1, subset to Latin + punctuation +
arrows/math, self-hosted as woff2 in `ui/fonts/`, `font-display: swap`,
size-adjusted fallback faces to cancel layout shift.

**A11. Textual theme** `orchestral-paper` and `orchestral-stage` registered in
`orchestral/tui/app.py` via `textual.theme.Theme` + `register_theme()`, values
copied from the token file (section 6.1).

**A12. README header**: replace the `<img>` of `social.png` with the A2 lockup as
SVG (`<picture>` with light/dark sources via `#gh-dark-mode-only` /
`prefers-color-scheme`) and a real observatory screenshot (paper theme) below the
first section, captured by `shots.py`.

---

## 4. Research and references

### 4.1 Category references (what "best in class" does)

| Product | What it does best | What we take |
|---|---|---|
| Braintrust experiment compare ([docs](https://www.braintrust.dev/docs/evaluate/compare-experiments)) | Pick a baseline, align test cases, add score deltas to every row, sort by regressions, inline or side-by-side diff | Compare becomes baseline-first; "order by regression" is the default sort; diff of artifacts side-by-side |
| Langfuse trace timeline and agent graphs ([timeline](https://langfuse.com/changelog/2024-06-12-timeline-view.md), [agent graphs](https://langfuse.com/docs/observability/features/agent-graphs)) | Whole trace on one screen, zooms like a map, color carries observation type; aggregated vs expanded graph | Run detail gets a single-screen timeline; we encode phase by **lane**, not color, and keep color for verdicts |
| Arize Phoenix experiment compare ([release note](https://arize.com/docs/phoenix/release-notes/08-2025/08-15-2025-enhance-experiment-comparison-views.md)) | Side-by-side metrics: evals, cost, latency, tokens per experiment | Cost and latency sit next to quality in every comparison, never on a separate page |
| Carbon data visualization ([color palettes](https://carbondesignsystem.com/data-visualization/color-palettes/)) | Palettes measured pairwise for contrast, strict sequence, 3:1 for meaningful graphics | We adopt the measure-every-pair discipline and the 3:1 graphics rule |
| Okabe and Ito CUD palette ([reference](https://conceptviz.app/blog/okabe-ito-palette-hex-codes-complete-reference)) | 8 colors distinguishable under all common CVD | Source hues for pass/fail/judge/live |
| Wilke, *Fundamentals of Data Visualization*, ch. 16 "Visualizing Uncertainty" ([O'Reilly](https://oreilly.com/library/view/fundamentals-of-data/9781492031079)) | Intervals and frequency framing for point estimates | Every rate is drawn with its Wilson interval; "8 of 10" framing beside percentages |

Design-system references read from the local corpus
(`~/Documents/github/personal/awesome-design-md/design-md/`): `ibm/DESIGN.md`
(Carbon: flat-square 0-4px corners, Plex, one assertive accent, tiles with no
shadow), `clickhouse/DESIGN.md` (near-black canvas, accent used scarcely, stat
numbers as the hero), `linear.app/DESIGN.md` and `vercel/DESIGN.md` (density,
monochrome UI chrome, keyboard-first), `sentry/DESIGN.md` and `warp/DESIGN.md`
(read and rejected: violet midnight canvas and warm charcoal + Instrument Serif
are each a known AI default for dev tools).

### 4.2 The "highest form" for this category

An eval observatory at its best is **a lab notebook that can be published**:

1. Every number is one click from the raw evidence that produced it (artifact,
   transcript, call log, hash).
2. Uncertainty is visible by default and impossible to ignore (intervals, n,
   hatching), so the UI cannot be screenshotted into a misleading claim.
3. Independent axes are visually independent (mechanical, judge, cost never share
   a color or a chart axis).
4. Spend is a first-class safety concern: anything that costs money shows its
   estimate and requires an explicit act.
5. Live runs are watchable without staring: state changes are announced, not
   blinked.
6. The same vocabulary, glyphs and verdict encodings appear in web, PNG, TUI and
   CLI, so a screenshot from any surface is recognisably orchestral.

### 4.3 Reference lock

```text
Primary reference/direction: IBM Carbon (via ibm/DESIGN.md + Carbon data-viz docs),
  reinterpreted as a printed orchestral score / engraved instrument plate.
Preserve: flat-square geometry (0-2px radius); tiles separated by hairlines, no
  shadows; one assertive non-data color budget (here: ink itself); measured
  data-viz palette with 3:1 graphics contrast; dense tabular rhythm.
Borrow only: (1) ClickHouse: the stat number as the hero of a card, accent used
  scarcely; (2) Linear: keyboard-first command palette and density in lists.
Role rules: semantic colors are evidence-only (pass/fail/judge/live) and never
  decorate chrome; hatch = low-n/uncertain only; mono = data, ids, slugs, code only;
  baton mark = brand only.
Media strategy: no stock, no generated bitmaps; brand assets are hand-drawn SVG;
  OG/cards are rendered from real data through the app's own CSS; empty states use
  the "Rests" SVG set.
Reject: Tailwind cyan/emerald/indigo defaults; dark-by-default; pill chips;
  glows and pulsing dots; gradient cards; treble-clef/emoji branding; warm cream +
  serif "calm editorial"; Instrument Serif; Inter as identity.
Token commitments: paper canvas #F7F8F8 / stage canvas #0E1012; ink #121417 /
  #ECEEF0; Instrument Sans + IBM Plex Mono; radius 2px controls, 0px tiles; 1px
  hairlines; Okabe-Ito-derived pass #009E73, fail #D55E00, judge #0072B2, live #E69F00.
```

### 4.4 Chosen direction: "The Score"

The product is literally called orchestral and its core object is a conductor
directing players. A printed orchestral score is the most information-dense
document in music: parallel staves, a shared time axis, engraved type, black ink
on paper, and *no decoration*. Every mark means something. That is exactly the
standard an eval tool should be held to.

Concretely:
- **Paper and ink.** Monochrome chrome. Brand presence comes from type, the
  baton mark and the staff-line motif, not from a colored accent. Color is reserved
  for evidence (rule: "if it is colored, it is data").
- **Staves.** Parallel lanes are the native layout for runs: the run detail
  timeline draws the orchestrator and each worker as staff lanes over time; the
  leaderboard strip plot draws pairings as lanes; group cards show replicate
  outcomes as tick marks on a line, like notes.
- **Engraving.** Flat 0-2px corners, 1px rules, tabular mono numerals, small caps
  only where a score would use them (column heads), never as section eyebrows.
- **Rests.** Silence is designed: empty states are first-class.

Why not the alternatives:
- *Keep "dark instrument workbench, cyan accent"*: it is already the generic
  dark-dev-tool centroid; it fails the identity test (swap the wordmark and it is
  any observability tool).
- *Acid accent on black (ClickHouse-like)*: strong, but it is someone else's
  signature and it would compete with the evidence colors for attention.
- *Warm editorial*: wrong audience; explicitly an AI default.

Light vs dark: **paper is the default** for published and hosted surfaces
(cards, OG, README, Cloudflare observatory, static reports), honoring the
anti-slop rule that dark must be justified. **Stage (dark) is first-class** and
follows `prefers-color-scheme` for the local observatory and TUI, because the
operator lives next to a terminal and watches long live runs at night. A
toggle in the rail overrides and persists in `localStorage`.

### 4.5 Decision ledger

| Decision | Source | Source rule / role | Why |
|---|---|---|---|
| Monochrome chrome, color = evidence | Carbon one-accent discipline; anti-slop token-role rule | semantic colors never decorate | Three axes + live state already need four hues; a brand hue would be a fifth competing signal |
| Paper default, stage first-class | anti-slop #3; operator context | dark must be justified | Published artifacts read better and print; operator gets dark by OS preference |
| Okabe-Ito hues for pass/fail/judge/live | Okabe and Ito CUD; Carbon 3:1 | meaningful graphics >= 3:1 | Current green/red pair is CVD-hostile |
| Shape + fill encodes verdict, color reinforces | `tui/state.py` comment "text carries the meaning"; WCAG 1.4.1 | color never sole carrier | Works in TUI, CLI, grayscale PNG |
| Hatch = low n | Wilke ch. 16; current amber misuse | texture reserved for uncertainty | Separates sample size from outcome |
| Instrument Sans | user brief (supreme, custom), taste skill (avoid Inter), Instrument Sans width axis | UI + display | Condensed widths for dense tables, one family for everything sans |
| IBM Plex Mono | Carbon reference | data, ids, code only | Tabular, slashed zero, distinct `1lI`, OFL |
| 2px radius controls / 0px tiles | Carbon flat-square | one documented radius rule | Replaces 6/12/18/99px mix |
| Baton mark, staff motif | product name and domain (conductor -> players) | brand only | Ownable, meaningful, scales to 16px |
| Baseline-first compare, sort by regression | Braintrust compare docs | compare pattern | Answers "did it get worse" first |
| Single-screen run timeline with lanes | Langfuse timeline | trace view | Shows parallel workers and where time/cost went |
| Cost estimate + explicit confirm for paid actions | product constraint (metered evals); flow audit 2.5 | safety | Two current one-click paid paths |
| No em dash in UI copy; designed null glyph | design-taste-frontend 9.G | copy rule | Em dash is today's null value everywhere |

---

## 5. Information architecture (proposed)

Keep all current routes (slugs are stable for links and `shot.png` captures).
Regroup the rail:

```
[mark] orchestral                (theme toggle)  (cmd-K)
  Now            #/              what is running, what changed, what needs a look
  Runs           #/runs
  Pairings       #/leaderboard   (label renamed; route unchanged)
  Compare        #/compare
  Experiments    #/experiment    (NEW: the A/B ledger moves off Overview)
  Publish        #/cards         (label renamed; route unchanged)
  Models         #/models
  ---
  New run        #/new           (hidden on hosted/read-only builds)
  Guide          #/about         (also reachable as inline "?" popovers)
Activity (live jobs, collapsible)
```

Global: `cmd/ctrl-K` command palette (jump to run id, task, pairing, group);
`g o / g r / g p / g c / g e` sequences; `/` focuses the current page filter;
`?` shows the key map (parity with the TUI bindings in `tui/app.py`).

---

## 6. Design system specification

All tokens live in one file, `ui/tokens.css`, consumed by `app.css`, the card
capture template, `render.py` (inlined at build-free import time), and mirrored
into the Textual theme and the reporter. `reporter.py` stops carrying its own
palette.

### 6.1 Color tokens

Naming: role first, never hue. Two themes: `paper` (default) and `stage`.

**Neutrals**

| Token | Paper | Stage | Use |
|---|---|---|---|
| `--canvas` | `#F7F8F8` | `#0E1012` | page background |
| `--surface` | `#FFFFFF` | `#15181B` | tables, panels, inputs |
| `--sunken` | `#EEF0F1` | `#0A0C0E` | code, transcripts, wells |
| `--raised` | `#FFFFFF` | `#1C2024` | popovers, palette, menus |
| `--rule` | `#E2E5E8` | `#262B30` | hairlines between rows |
| `--rule-strong` | `#C9CED3` | `#394047` | section rules, staff lines |
| `--control-border` | `#858C94` | `#5F6770` | input/button outlines (>=3:1 vs surface) |
| `--ink` | `#121417` | `#ECEEF0` | primary text, brand mark, primary button fill |
| `--ink-2` | `#4A5159` | `#A9B0B8` | secondary text |
| `--ink-3` | `#69717A` | `#838B94` | tertiary text, axis labels (still AA) |
| `--on-ink` | `#FFFFFF` | `#0E1012` | text on ink fills |

**Evidence colors** (Okabe-Ito derived; `-fill` for marks and bars, `-text`
for text on canvas/surface, `-wash` for 8-12% backgrounds)

| Token | Paper fill / text | Stage fill / text | Meaning (only) |
|---|---|---|---|
| `--pass` | `#009E73` / `#00775A` | `#2EC495` / `#2EC495` | mechanical pass |
| `--fail` | `#D55E00` / `#B04A00` | `#F2813F` / `#F2813F` | mechanical fail, errors |
| `--judge` | `#0072B2` / `#005A8F` | `#6CC0EE` / `#6CC0EE` | judge axis (any judge value) |
| `--live` | `#E69F00` / `#8A5F00` | `#F0B429` / `#F0B429` | running, in-flight, pending spend |
| `--focus` | `#121417` ring on `#FFFFFF` halo | `#ECEEF0` ring on `#0E1012` halo | keyboard focus (2px + 2px offset) |

There is no "warn" color. Today's amber `--warn` carries four meanings (running,
low-n, inconclusive, partial); each gets its own encoding: running = `--live`;
low-n = hatch; inconclusive = half-filled glyph in `--ink-2`; partial = progress
fraction text.

**Contrast (measured, WCAG 2.x)**

| Pair | Paper | Stage |
|---|---|---|
| `--ink` on canvas / surface | 17.3 / 18.5 | 16.4 / 15.3 |
| `--ink-2` on canvas / surface | 7.6 / 8.0 | 8.7 / 8.1 |
| `--ink-3` on canvas / surface | 4.65 / 4.95 | 5.5 / 5.2 |
| `--pass-text` | 5.2 / 5.6 | 8.6 / 8.0 |
| `--fail-text` | 5.2 / 5.5 | 7.3 / 6.8 |
| `--judge-text` | 6.9 / 7.3 | 9.5 / 8.8 |
| `--live-text` | 5.3 / 5.7 | 10.2 / 9.6 |
| `--pass-fill` / `--fail-fill` vs surface (graphics, need 3:1) | 3.4 / 3.9 | pass |
| `--control-border` vs surface | 3.2 | 3.1 |
| text on fills | `--ink` on pass-fill 5.4, on fail-fill 4.8, on live-fill 8.2; white on judge-fill 5.2 | use `--on-ink` |
| Current product for comparison | `--text-3 #57636f` on `#0b0d10` = **3.17** (fails) | |

Pass and fail fills differ in luminance by only 1.13:1, so they must never be
the sole distinction (protanopia safety comes from the hue pair, achromatopsia
and print from glyph shape). Rule: every pass/fail mark carries its glyph or text.

### 6.2 Typography

Families:

```css
--font-sans: "Instrument Sans", "Instrument Sans Fallback", ui-sans-serif, sans-serif;
--font-mono: "IBM Plex Mono", "Plex Mono Fallback", ui-monospace, monospace;
/* fallbacks are @font-face local() aliases with size-adjust/ascent-override
   tuned so the swap causes no layout shift */
```

Feature defaults: `font-variant-numeric: tabular-nums` on every number;
`font-feature-settings: "zero"` for Plex Mono ids/hashes.

Scale (UI is 13px-based for density; card/OG scale separate):

| Token | Size / line | Family, weight, width | Use |
|---|---|---|---|
| `display-l` | 56 / 60 | sans 600, wdth 90, -0.02em | OG, card hero numbers (mono variant for numerals) |
| `display-m` | 36 / 40 | sans 600, wdth 92, -0.015em | card title, empty-state titles at large |
| `title-l` | 22 / 28 | sans 600, -0.01em | page title |
| `title-m` | 17 / 24 | sans 600 | section title (sentence case, no eyebrow) |
| `title-s` | 14 / 20 | sans 600 | panel/table titles |
| `body` | 13 / 20 | sans 400 | default UI text |
| `body-l` | 15 / 24 | sans 400 | Guide/About prose, max 68ch |
| `label` | 12 / 16 | sans 500 | form labels, chips |
| `col-head` | 11 / 14 | sans 600, wdth 80, +0.04em, uppercase | **table column heads only** |
| `data` | 12.5 / 18 | mono 400 | table numerics, slugs, ids |
| `data-strong` | 12.5 / 18 | mono 600 | ranked numbers, deltas |
| `metric` | 28 / 32 | mono 500, -0.02em | stat blocks in app |
| `micro` | 11 / 14 | mono 400 | axis ticks, timestamps (min size in UI) |

Rules: nothing under 11px. Uppercase only for `col-head` and the scope chip on
cards. Section titles are sentence case. Slugs are shortened by a shared
`shortSlug()` (drop vendor prefix, keep version) with the full slug in a
tooltip and in copy actions. `text-wrap: balance` on titles; `pretty` on Guide
prose.

### 6.3 Spacing

4px base. Tokens: `--s-1 4`, `--s-2 8`, `--s-3 12`, `--s-4 16`, `--s-5 24`,
`--s-6 32`, `--s-7 48`, `--s-8 64`.
- Table row height: 32px default, 28px "compact" (toggle, persisted), 40px touch.
- Page gutter: 24px desktop, 16px at <=640px.
- Content max width: none for tables (they use the screen), 72ch for prose.
- Vertical rhythm between page sections: 32px with a `--rule-strong` hairline, not
  boxes.

### 6.4 Radius

| Element | Radius |
|---|---|
| Tiles, tables, panels, cards, charts | 0 |
| Buttons, inputs, selects, chips, tooltips, popovers | 2px |
| Focus ring | follows element |
| Live dot, judge gauge | geometric (circle) only where the glyph is a circle |

No pills anywhere. The 1200x675 share card has 0 radius (the platform crops).

### 6.5 Elevation

Flat by default: separation by `--rule`. Exactly two raised layers:
1. Popover / menu / command palette: `--raised` + 1px `--rule-strong` + shadow
   `0 8px 24px rgb(18 20 23 / .10)` (paper) or `0 8px 24px rgb(0 0 0 / .45)` (stage).
2. Modal / confirm sheet: same, plus a scrim `rgb(14 16 18 / .4)`.

No shadows on cards, tables, buttons or charts. No gradients anywhere in the UI.

### 6.6 Layout grid

- Desktop shell: 232px rail + fluid main. Rail collapses to a 56px icon rail at
  <=1100px (icons from A4 with tooltips and `aria-label`) and to a bottom tab bar
  (5 items: Now, Runs, Pairings, Publish, More) at <=640px.
- Main uses a 12-col grid with 24px gutters for dashboards; tables span 12.
- Sticky page header (title + primary filter + primary action) at 56px.
- Tables: sticky header row, sticky first column on horizontal overflow, column
  priority attributes (`data-pri="1..3"`) so priority-3 columns hide first at
  narrow widths and are reachable through a row-expand.

### 6.7 Motion

Principle: motion only explains a state change. Durations 120ms (micro), 200ms
(panel), 320ms (route); easing `cubic-bezier(.2,0,0,1)` (out) and
`cubic-bezier(.4,0,1,1)` (in).

| Motion | Spec | Replaces |
|---|---|---|
| Baton (live indicator) | the A1 baton stroke inside a 12px ring sweeps 0-30 degrees and back once per 2.4s, ease-in-out; no opacity blink | infinite `pulse` opacity blink on every dot |
| Phase advance | when a run phase completes, its lane segment fills left-to-right over 200ms and the next segment's label weight changes 400 -> 600 | none |
| Number tick | live cost/tokens change with a 120ms vertical slide of the changed digits only | none |
| Row insert | streamed event rows enter with 4px translate + `--live` wash fading over 1.2s | none |
| Route change | 120ms crossfade of `#view`; skeleton rows match final row height | text "Loading..." |
| Press | `translateY(1px)` on `:active` for buttons | none |

`@media (prefers-reduced-motion: reduce)`: all of the above become instant;
the baton becomes a static filled dot plus the word "live". Live updates are also
announced through an `aria-live="polite"` region (phase changes and completion
only, never every event).

### 6.8 Iconography and glyph parity

A4 set on web. Terminal parity table (TUI uses Rich markup with theme colors;
CLI uses the same glyphs, colors only when `isatty` and `NO_COLOR` unset):

| Meaning | Web | TUI / CLI glyph | Color (if allowed) |
|---|---|---|---|
| pass | filled square + check | `■ pass` | pass |
| fail | hollow square + slash | `□ fail` | fail |
| cancelled | dashed square | `┄ cancelled` | ink-2 |
| running | baton ring | `◔ live` | live |
| inconclusive | half square | `◧ inconclusive` | ink-2 |
| not judged | hollow circle | `○ not judged` | ink-3 |
| judge score | gauge + number | `◖0.83` | judge |
| low n | hatch square | `░ low n` | ink-2 |
| flag: interesting | fermata | `𝄐`-shaped icon on web; `* flagged` in terminal | ink |

CLI tables: use `rich.table.Table` (already a dependency) with column overflow
`fold`/ellipsis, `shortSlug()`, durations through the same formatter as the web
(`7.4m`, `612ms`), and a `low n` marker. One formatter module shared by web
state, TUI and CLI: `orchestral/format.py` (proposed).

### 6.9 Data visualization rules

1. **Axes never share a channel.** Mechanical = pass/fail hues. Judge = judge
   blue ramp only. Cost = ink/gray only, always on its own axis or as text.
2. **Rates come with intervals.** Every pass rate is drawn as a point plus its
   Wilson 95% interval line; text shows "8 of 10 (80%)" before percentage alone.
3. **Hatch means uncertain.** n below threshold (currently 3 for cells, 10 for
   leaderboard "best") renders with the hatch pattern at reduced fill, plus the
   `low n` glyph in text. Never a hue.
4. **Heatmaps** (tasks x pairings, orchestrator x worker): one sequential scale for
   mechanical pass rate. Not a red-to-green diverging scale (diverging implies a
   meaningful midpoint, and there is none); instead a single-hue sequential ramp from `--sunken` to `--pass-fill` in 6 steps, with the
   percentage printed in the cell (ink on light steps, on-ink on dark steps,
   switched by computed contrast). Never-attempted cells: empty with a 1px
   `--rule` diagonal; low-n: hatch over the fill. Cells are `<a>` elements.
5. **Judge ramp** (6 steps, paper): `#E3F0F8 #B9DAEE #84BEE0 #4C9CCB #0072B2 #004C77`;
   stage: `#16242E #1D3A4F #255673 #3277A0 #4F9DCB #6CC0EE`.
6. **Leaderboard plot**: replace the bubble scatter with a **strip plot by lane**
   (each pairing is a staff lane, rows sorted by the active lens): pass-rate
   interval on a shared 0-100% axis on the left half, cost per pass on a log axis
   with labeled ticks ($0.001, $0.01, $0.1) on the right half. Labels are the row
   labels, so they never collide. This is the signature chart of the product.
7. **Run timeline**: lanes = orchestrator (plan), each worker, assemble, validate,
   judge. Bars = calls, length = latency, a 2px top tick colored by verdict of that
   call (pass/fail) or `--live` while in flight; cost shown as text at bar end on
   hover/focus. Shared time axis with labeled ticks.
8. **Failure taxonomy**: horizontal bars in `--ink-2`, sorted, each bar links to
   the filtered run list. Red is not used (all of them are failures; color adds
   nothing).
9. **Categorical series** (only where needed, e.g. reporter multi-pairing plots):
   Okabe-Ito order `#0072B2 #E69F00 #009E73 #CC79A7 #56B4E9 #D55E00 #F0E442`, max 7,
   with direct labels instead of legends.
10. **Accessibility**: every SVG chart has `role="img"`, a `<title>`, a `<desc>`
    with the key numbers, and a "View as table" toggle. Interactive marks are
    focusable (`tabindex="0"`, Enter to open).
11. Number formats: money `$0.0072` below $0.01, `$0.187` below $1, `$12.40` above;
    percentages integers; scores two decimals; durations via the shared
    formatter; null = the **null glyph** (a 10px `--ink-3` rule, `aria-label="no data"`),
    never an em dash.

### 6.10 Component inventory with states

Every interactive component specifies: default, hover, focus-visible, active,
disabled, loading; data components add empty, partial, error, live.

| Component | Anatomy | States and rules |
|---|---|---|
| Button (primary) | ink fill, on-ink label, 32px, 2px radius | hover: ink at 88%; active: translateY(1px); focus: ring; disabled: `--rule-strong` fill, ink-3 label; loading: label stays, trailing baton spinner |
| Button (secondary) | surface, `--control-border` 1px | hover: border ink; others as primary |
| Button (spend) | primary + price tag segment ("Launch · est. $0.12") | requires confirm sheet when est. > $0 or estimate unknown; disabled while estimate loads |
| Icon button | 28px square, icon 16 | tooltip on hover/focus, `aria-label` required |
| Input / select / combobox | label above, 32px, helper and error below | error: fail-text message + 1px fail border; combobox for tasks/models with type-ahead and grouping by type/role |
| Checkbox / toggle | 16px square, 2px radius | checked: ink fill + on-ink check |
| Verdict chip | glyph + word, 22px, 2px radius, 1px border in semantic color, wash background | pass, fail, cancelled, running, inconclusive, not judged, not judgeable, judge unknown: each has its glyph (6.8) |
| Judge value | gauge glyph + mono score + calibration word ("advisory" / "calibrated k=0.74") | uncalibrated is always labeled |
| Interval bar | point + whisker on hairline track (no filled background track) | low-n: hatched whisker |
| Stat block | col-head label, metric number, detail line; no box | live: number tick motion |
| Data table | col-heads, 32px rows, sticky head/first col, row hover `--sunken` | selection: 2px ink inset on left (means "selected", allowed stripe), keyboard row nav (j/k), empty: Rest illustration + action, loading: skeleton rows, error: inline banner with retry |
| Filter bar | search + facet chips ("group: jev-ab", removable) + saved views | debounced 200ms; URL-synced |
| Tabs | underline 2px ink, `role="tablist"` | counts in tabs ("Events 214"); empty tabs dimmed but focusable |
| Phase lane timeline | see 6.9 #7 | empty: Rest #3 "Run is starting"; live: baton on current lane |
| Event stream | mono rows, time, type, worker, summary; expandable detail | `button` rows with `aria-expanded`; follow-tail toggle; live row insert motion |
| Artifact viewer | toolbar (viewport 375/768/1280, open raw, copy path), framed iframe on `--sunken` | missing: Rest #4 with the reason from `judge_reason`/report |
| JSON viewer | collapsible tree, copy path, search | replaces raw `<pre>` dumps for report/plan/manifest |
| Heatmap cell | `<a>` with % text, ramp fill, hatch if low n | focus ring, tooltip with n and judge mean |
| Lane strip plot | see 6.9 #6 | selection syncs with table row |
| Flag control | single icon button cycling none -> interesting (fermata) -> dismissed (rest) -> none | label in tooltip and `aria-pressed`; shown on row hover/focus only, always visible when set |
| Command palette | `cmd-K`, raised layer, fuzzy over runs/tasks/pairings/groups/commands | recent items, keyboard only |
| Toast | bottom-left, raised, 4s, only for transient confirmations (copied, flag saved) | errors are never toasts |
| Confirm sheet (spend) | modal: what will run, n calls, estimate range, budget remaining, "Run dry first" secondary | Enter does not confirm a paid action; requires explicit click or typed "run" |
| Program card (A6) | section 3.2 | fluid in-app, fixed at capture; no-proof collapse |
| Empty state | Rest SVG 64px, title-s, one line, one action | five variants (A7) |
| Live job item (rail) | baton + label + elapsed + spend so far | click opens run; cancel in context menu |

---

## 7. Screen-by-screen redesign plan

Priority: **P0** = fixes correctness, safety, accessibility or the identity
foundation; **P1** = the redesign proper; **P2** = supreme-ceiling polish.

### 7.0 Foundation (P0)

- `ui/tokens.css`, fonts, `ui/icons.svg`, brand SVGs, favicon set, meta tags
  (`theme-color` per scheme, OG for hosted build), theme toggle.
- Shared formatter (`orchestral/format.py` + mirrored `fmt*` in `app.js`), null glyph,
  no em dashes in UI strings, `shortSlug()`.
- Accessibility pass: tabs roles, row buttons, focusable chart marks and heatmap
  cells, `aria-live` region, focus rings, `--ink-3` contrast fix.
- Spend safety: dry run default ON in New run; spend button + confirm sheet;
  thread writer defaults to blank (template) and shows an estimate before any paid
  call.

### 7.1 Now (Overview)

- Pragmatic first pass (P1): three bands, top to bottom: **Live** (running jobs
  as staff lanes with phase, elapsed, spend so far, cancel), **Changed since you
  last looked** (runs finished since the last visit, stored in `localStorage`:
  counts by verdict, new failures with their taxonomy reason, cost spent),
  **Needs a look** (inconclusive judges, infra errors, flagged items, pricing drift
  from `prices`). Then the tasks x pairings heatmap (6.9 #4, sticky task column,
  sentence-case titles). Groups become a compact table with human labels (auto-label
  rule: `experiment · task · orch / worker · arm`). Experiment ledger moves to
  `#/experiment` with a one-line summary link here.
- Supreme ceiling (P2): the heatmap becomes a zoomable small-multiples grid
  grouped by task family (v2, v3, router-eval), with a diff mode against a chosen
  date ("what moved this week").

### 7.2 Runs

- P1: paginated/virtualized table (200 rows per page, keyset on `started_at`),
  facet chips (group, task, pairing, verdict, judge state, type, difficulty) in the
  URL, one search box, saved views, row keyboard nav, bulk export. Group column
  shows labels. Clicking a heatmap cell lands here with **both** task and pairing
  facets set.
- P2: column chooser, density toggle, inline sparkline of cost per run over time.

### 7.3 Run detail

- P1: header in one column: task title, pairing (`orch / worker`), group label,
  then a single stat row (Verdict, Judge, Cost, Tokens, Duration, Started). For
  failed runs a **failure summary** block directly under the header: taxonomy
  reason, the failing check, the first error event, link to it. Then the lane
  timeline (6.9 #7). Tabs: Artifact (viewer), Events (stream), Calls (table with
  per-call cost and pricing source), Report (JSON viewer + rendered check list),
  Review, Plan (rendered as the subtask list, JSON toggle), Manifest (hashes with
  copy).
- P2: side-by-side "compare to another replicate of this cell" with artifact diff
  (Braintrust pattern); keyboard `[`/`]` to step through replicates.

### 7.4 Pairings (Leaderboard)

- P1: default scope = **All groups** (or the most recent group with >=3 pairings),
  never a degenerate cohort; lens tabs remain but a lens with one eligible row says
  so instead of five identical selections. Signature lane strip plot (6.9 #6)
  synced with the ranking table; orchestrator x worker matrix with the 6.9 #4 ramp;
  ranking table with column priorities and no wrapping ranks.
- P2: "frontier" overlay: the Pareto set on pass-rate-lower-bound vs cost per pass
  highlighted with an ink outline and explained in one sentence.

### 7.5 Compare

- P1: baseline-first (Braintrust pattern): pick baseline group, candidate group;
  summary row of improved / regressed / stable / one-sided with counts; table
  sorted by regression magnitude; each delta drawn as a dumbbell (baseline point
  to candidate point with both intervals) instead of `+12pp` text; cost delta
  column.
- P2: per-cell drill into paired runs with artifact diff.

### 7.6 Experiments (new route, content moved from Overview)

- P1: the A/B ledger with done/partial/pending/aborted grouped and collapsible
  (pending collapsed by default), per-arm interval dumbbells, budget gauge (spend vs
  `--budget` and daily cap), posted marks.
- P2: sequential-analysis view showing the difference CI narrowing per batch.

### 7.7 Publish (Cards gallery + card)

- P1: gallery as a table-like list with a large preview on selection (no card
  grid); card editor with feed-size preview (a 600px-wide simulated timeline width
  so the author sees it as readers will), alt text generated and editable,
  download PNG 2x, copy alt, copy caption. Program card layout (A6), no clipping,
  no-proof collapse.
- P2: series consistency check (warns if a card's claim is unsupported by n, e.g.
  "best" on low-n); export a set of cards as a zip with a manifest.

### 7.8 New run

- P1: task combobox grouped by type/family with difficulty and expected cost from
  history; pairing pickers show qualified roles from the model catalog; dry run ON
  by default; live estimate (calls x priced tokens from history, range); spend
  confirm sheet. Hidden entirely on hosted builds.
- P2: "re-run this cell" shortcut from any run/cell, prefilled.

### 7.9 Models

- P1: keep the table; add capability icons, role usage as small bars, provider
  sync state as a stamped line at the top.
- P2: per-model cost/quality history across roles (small multiples).

### 7.10 Guide (About)

- P1: keep the copy, typeset as `body-l` prose with the Rests/glyph legend; the
  same definitions surface as `?` popovers next to "Judge", "Low n", "CI".

### 7.11 Static reports, error pages, TUI, CLI, README

- P0: `render.py` error pages use tokens + Rest #5; no em dash in titles.
- P1: `reporter.py` HTML reuses `ui/tokens.css` (read and inlined, so reports stay
  single-file) and the chart rules (no mixed judge/mech axis). TUI registers
  `orchestral-paper`/`orchestral-stage` themes and the glyph table. CLI tables move
  to `rich.table` with the shared formatter. README gets A12.
- P2: TUI lane timeline in the Live screen using block characters on the same
  lane model as the web.

### 7.12 Pragmatic first pass vs supreme ceiling

| | First pass (ship in ~3 PRs) | Supreme ceiling |
|---|---|---|
| Identity | tokens, fonts, mark, wordmark, favicon, icons, OG | custom `t` wordmark glyph refined by hand, printed style sheet for reports |
| Data viz | heatmap ramp + hatch, interval bars, lane strip plot | zoomable small multiples, Pareto overlay, sequential-analysis view |
| Flows | Now screen, facet runs, spend safety, failure summary | replicate stepping, artifact diffs, command palette with actions |
| Surfaces | SPA + error pages + cards | reporter, TUI themes, CLI parity, hosted read-only build |
| Motion | baton, reduced-motion, aria-live | number ticks, phase-fill, row-insert washes |

Suggested PR sequence: (1) foundation tokens/fonts/brand/a11y/spend safety,
(2) Now + Runs + Run detail, (3) Pairings + Compare + Experiments + charts,
(4) Publish/cards + OG + README, (5) reporter + TUI + CLI parity.

---

## 8. Accessibility budget

- WCAG 2.2 AA minimum everywhere; all text >= 4.5:1 (measured table in 6.1),
  meaningful graphics and control borders >= 3:1.
- Color is never the sole carrier (glyph + word for every verdict; hatch +
  "low n" text for uncertainty).
- Full keyboard path for every task in 2.5; visible 2px focus ring with offset;
  skip link to main; logical heading order (one `h1` per view).
- Tabs, disclosure rows, combobox, dialogs use correct ARIA patterns; dialogs trap
  focus and restore it.
- Charts: `role="img"` + `desc` + table toggle; focusable marks.
- Live regions: polite, phase-level only.
- Targets >= 24x24px (WCAG 2.5.8), 40px row height in touch layout.
- `prefers-reduced-motion`, `prefers-contrast: more` (rules go to `--rule-strong`,
  ink-3 promoted to ink-2), `forced-colors` (glyphs use `currentColor`, hatch via
  SVG stroke so it survives).
- Images: artifact `<img>` gets alt from task title; cards ship generated alt text.
- Zoom to 200% and 320px reflow without horizontal page scroll (tables scroll
  inside their container with sticky first column).

## 9. Performance budget

| Item | Budget |
|---|---|
| Fonts | 2 families, <= 4 woff2 files, <= 110KB total, Latin subset, preload only Instrument Sans regular |
| CSS (tokens + app) | <= 45KB uncompressed, no framework |
| JS (`app.js`) | <= 90KB uncompressed today-equivalent; split card/compare/experiment views into lazily imported modules if it exceeds |
| Icons + brand SVG sprite | <= 20KB |
| First render of Now on local data (150 runs) | < 400ms after `DOMContentLoaded`; < 1s TTI on hosted Worker |
| Runs list | virtualized/paginated; DOM <= 1,500 nodes for the table |
| Polling | one shared poller (5s idle, 1-2s only while a live run is on screen), paused when `document.hidden` |
| OG / card PNG | <= 150KB at 1200x630/675 |
| CLS | < 0.02 (size-adjusted font fallbacks, fixed chart heights, skeletons sized to rows) |

## 10. Copy rules

- No em dash or en dash in UI strings; use a period, comma, colon or parentheses.
  Ranges use a hyphen ("49-94%").
- At most one `·` per metadata line; prefer columns or line breaks.
- Sentence case everywhere except `col-head`.
- Name things the way the product does: "pairing", "orchestrator / worker",
  "mechanical", "judge", "advisory", "calibrated", "low n". Never "AI", never
  "seamless/elevate/unlock".
- Every empty state says what is missing and the one thing to do next
  (for example: "No runs in this group yet. Launch a dry run to see the pipeline
  end to end." with a "New dry run" action).

## 11. Anti-slop gate (checked against this spec)

- Accent is not indigo/violet; there is no brand hue at all. Pass.
- Cards: only the share card (an export object) and popovers are containers;
  dashboards use rules and tables. Pass.
- No decorative side stripes; the only left inset is row selection. Pass.
- No emoji; the clef emoji in `social.png` is removed. Pass.
- Light is default for published surfaces; dark is preference-driven and
  justified. Pass.
- No serif, no warm cream, no one-word italic swaps. Pass.
- Reference traits preserved (flat-square, one-accent discipline, measured palette)
  rather than averaged. Pass.
- Token roles fixed (evidence colors, hatch, mono, mark). Pass.
- Media roles: all imagery is real data or hand-drawn SVG; no fake UI in OG. Pass.
- Eyebrows: removed from sections; `col-head` is a table role, not an eyebrow. Pass.

## 12. Open questions

1. **Hosted audience.** The Cloudflare plan keeps the Worker Access-gated. Will a
   scrubbed public read-only build exist? It changes whether OG images, a landing
   section and SEO meta are P1 or P2.
2. **Default theme for the local observatory.** Spec says follow OS preference,
   paper when none. Does the operator want stage forced locally?
3. **Wordmark custom `t`.** Approve the custom glyph, or ship the plain Instrument
   Sans wordmark first and refine later?
4. **Low-n thresholds.** UI uses n<3 for cells and n<10 for "best" in the
   leaderboard (`docs/tui-observability-design.md`); one threshold or two, and
   should the UI expose it?
5. **Thread writer.** Keep the paid "Write thread" feature at all in the hosted
   build (it cannot be read-only)? Recommendation: local-only, template default.
6. **Rename nav labels** ("Leaderboard" -> "Pairings", "Cards" -> "Publish",
   "About" -> "Guide"): routes stay, but labels are muscle memory. Approve?
7. **Reporter convergence.** Should `report --html` / `dashboard` eventually be
   replaced by a static export of the SPA itself (one renderer), or keep a separate
   single-file report that only shares tokens?
8. **Empty run artifacts in the local audit.** Recent jev-ab runs (for example
   `ca10bd3f61e4`) returned no artifact and an empty event stream from the API
   when served from a copied runs dir. Confirm whether that is path resolution in
   the copy or missing data; the redesign's failure/empty states depend on knowing
   which states are real.

---

Sources consulted: Braintrust compare docs
(https://www.braintrust.dev/docs/evaluate/compare-experiments), Langfuse
timeline (https://langfuse.com/changelog/2024-06-12-timeline-view.md) and agent
graphs (https://langfuse.com/docs/observability/features/agent-graphs), Arize
Phoenix compare release note
(https://arize.com/docs/phoenix/release-notes/08-2025/08-15-2025-enhance-experiment-comparison-views.md),
Carbon data-viz palettes (https://carbondesignsystem.com/data-visualization/color-palettes/),
Okabe-Ito reference (https://conceptviz.app/blog/okabe-ito-palette-hex-codes-complete-reference),
Wilke, Fundamentals of Data Visualization
(https://oreilly.com/library/view/fundamentals-of-data/9781492031079), Instrument
Sans (https://github.com/Instrument/instrument-sans), local
`awesome-design-md/design-md/{ibm,clickhouse,linear.app,vercel,sentry,warp}/DESIGN.md`.
