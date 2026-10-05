#!/usr/bin/env python3
"""Record the launch video's UI beats from the real observatory.

Builds the key-free fixture corpus (or reads one from ``--runs-dir``), serves
the paper-theme observatory on loopback and drives the beats listed in
``demo/fixtures/beats.json`` with Playwright. Playwright records video at the
viewport's pixel size and ignores device scale, so the viewport is the 1920x1080
video frame itself and the UI is zoomed 1.333x (a 1440x810 layout drawn at full
resolution, never upscaled). Each captured beat becomes one clean WebM
(Chromium draws no cursor into a recording). ``events.json`` carries what the
composition needs for a synthetic cursor and camera: a target rectangle per
``target`` or ``column`` step in video pixels and a timestamp in the beat's own
milliseconds.

    python demo/capture/observatory.py --out /tmp/launch-capture
    python demo/capture/observatory.py --out /tmp/x --dry-run     # no video, no waiting
    python demo/capture/observatory.py --out /tmp/x --beat b4-pairings

Event timestamps come from the planned clock (the sum of each beat's own holds
and scrolls), never from wall time, so events and their rectangles are identical
on every run. Only ``video_offset_ms`` (how long the recording ran before the
beat's t=0) is measured.
The script refuses to start when any ``*_API_KEY`` variable is set and writes
only under ``--out``, which belongs outside the repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BEATS = ROOT / "demo" / "fixtures" / "beats.json"
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
SCHEMA_VERSION = 1
OPS = {"hold", "target", "column", "scroll", "hover"}
KINDS = {"composed", "capture"}


class CaptureError(Exception):
    """A beat that cannot be recorded as written (missing selector, bad plan)."""


def key_variables(env: dict[str, str] | None = None) -> list[str]:
    """Names of provider-key variables set in ``env`` (default: the process)."""
    source = os.environ if env is None else env
    return sorted(k for k in source if k.upper().endswith("_API_KEY") and source[k])


def refuse_if_keys(env: dict[str, str] | None = None) -> None:
    names = key_variables(env)
    if names:
        raise SystemExit(
            "observatory capture refuses to start with provider keys in the environment: "
            f"{', '.join(names)}. Run it with `env -u NAME ...` for each; captures are key-free.")


# -- shot list ---------------------------------------------------------------

def load_beats(path: Path = BEATS) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate_beats(doc)
    if problems:
        raise CaptureError("beats.json is invalid: " + "; ".join(problems))
    return doc


def step_ms(step: dict[str, Any]) -> int:
    """Planned duration of one step: only holds and scrolls take time."""
    return int(step.get("ms", 0)) if step["op"] in ("hold", "scroll") else 0


def validate_beats(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if doc.get("version") != SCHEMA_VERSION:
        out.append(f"version must be {SCHEMA_VERSION}")
    vp = doc.get("viewport") or {}
    if not all(isinstance(vp.get(k), int) and vp[k] > 0 for k in ("width", "height")):
        out.append("viewport needs positive integer width and height")
    beats = doc.get("beats") or []
    ids = [b.get("id") for b in beats]
    if len(set(ids)) != len(ids):
        out.append("beat ids must be unique")
    cursor = 0.0
    for b in beats:
        bid = b.get("id", "?")
        if b.get("kind") not in KINDS:
            out.append(f"{bid}: kind must be one of {sorted(KINDS)}")
        if b.get("start_s") != cursor or b.get("end_s", 0) <= b.get("start_s", 0):
            out.append(f"{bid}: beats must tile the timeline with no gap or overlap")
        cursor = b.get("end_s", cursor)
        if not b.get("caption"):
            out.append(f"{bid}: every beat carries its caption (muted playback)")
        if b.get("kind") == "capture":
            if not b.get("route", "").startswith("/"):
                out.append(f"{bid}: capture beats need a route")
            planned = sum(step_ms(s) for s in b.get("steps", []))
            window = round((b["end_s"] - b["start_s"]) * 1000)
            if planned != window:
                out.append(f"{bid}: steps take {planned}ms but the beat is {window}ms")
            for s in b.get("steps", []):
                if s.get("op") not in OPS:
                    out.append(f"{bid}: unknown op {s.get('op')!r}")
    if beats and cursor != doc.get("total_s"):
        out.append(f"beats end at {cursor}s but total_s is {doc.get('total_s')}")
    return out


# -- events.json -------------------------------------------------------------

def validate_events(doc: dict[str, Any]) -> list[str]:
    """Problems with an ``events.json`` document; empty means valid."""
    out: list[str] = []
    if doc.get("version") != SCHEMA_VERSION:
        out.append(f"version must be {SCHEMA_VERSION}")
    vp = doc.get("viewport") or {}
    vw, vh = vp.get("width"), vp.get("height")
    if not (isinstance(vw, int) and isinstance(vh, int) and vw > 0 and vh > 0):
        return [*out, "viewport needs positive integer width and height"]
    if not isinstance(doc.get("zoom"), (int, float)) or doc["zoom"] <= 0:
        out.append("zoom must be a positive number")
    beats = {b.get("id"): b for b in doc.get("beats", [])}
    if not beats:
        out.append("no beats")
    for b in beats.values():
        for k in ("id", "start_ms", "end_ms", "kind"):
            if k not in b:
                out.append(f"beat missing {k}")
    for i, e in enumerate(doc.get("events", [])):
        where = f"events[{i}]"
        beat = beats.get(e.get("beat"))
        if beat is None:
            out.append(f"{where}: unknown beat {e.get('beat')!r}")
            continue
        span = beat["end_ms"] - beat["start_ms"]
        t = e.get("t_ms")
        if not isinstance(t, int) or not 0 <= t <= span:
            out.append(f"{where}: t_ms {t!r} is outside the beat (0 to {span})")
        if e.get("type") not in {"target", "scroll"}:
            out.append(f"{where}: type must be target or scroll")
        if not e.get("label"):
            out.append(f"{where}: label is required")
        r = e.get("rect") or {}
        x, y, w, h = (r.get(k) for k in ("x", "y", "w", "h"))
        if not all(isinstance(v, (int, float)) for v in (x, y, w, h)) or w <= 0 or h <= 0:
            out.append(f"{where}: rect needs numeric x, y and positive w, h")
        elif x < 0 or y < 0 or x + w > vw + 1e-6 or y + h > vh + 1e-6:
            out.append(f"{where}: rect {r} lies outside the {vw}x{vh} viewport")
    return out


# -- driving the page --------------------------------------------------------

_SCROLL_JS = """([sel, ms, block]) => new Promise((resolve, reject) => {
  const el = document.querySelector(sel);
  if (!el) return reject(new Error('no element for ' + sel));
  let host = el.parentElement;
  while (host && !(host.scrollHeight > host.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(host).overflowY))) host = host.parentElement;
  const scroller = host || document.scrollingElement;
  const box = el.getBoundingClientRect();
  const base = host ? host.getBoundingClientRect().top : 0;
  const view = host ? host.clientHeight : window.innerHeight;
  const want = block === 'start' ? box.top - base - 24 : box.top - base - (view - box.height) / 2;
  const from = scroller.scrollTop, to = Math.max(0, from + want), t0 = performance.now();
  const ease = p => 1 - Math.pow(1 - p, 4);
  const frame = now => {
    const p = Math.min(1, (now - t0) / ms);
    scroller.scrollTop = from + (to - from) * ease(p);
    p < 1 ? requestAnimationFrame(frame) : resolve();
  };
  requestAnimationFrame(frame);
})"""

_COLUMN_JS = """([table, col]) => {
  const t = document.querySelector(table);
  if (!t) return null;
  const cells = [...t.querySelectorAll('tr')].map(r => r.children[col - 1]).filter(Boolean);
  const rects = cells.map(c => c.getBoundingClientRect()).filter(r => r.width && r.height);
  if (!rects.length) return null;
  const x = Math.min(...rects.map(r => r.left)), y = Math.min(...rects.map(r => r.top));
  return {x, y, w: Math.max(...rects.map(r => r.right)) - x, h: Math.max(...rects.map(r => r.bottom)) - y};
}"""


def clip_rect(r: dict[str, float], vw: int, vh: int) -> dict[str, float] | None:
    """Intersect a rectangle with the viewport; None when nothing is visible."""
    x0, y0 = max(0.0, r["x"]), max(0.0, r["y"])
    x1, y1 = min(float(vw), r["x"] + r["w"]), min(float(vh), r["y"] + r["h"])
    if x1 <= x0 or y1 <= y0:
        return None
    return {"x": round(x0, 1), "y": round(y0, 1), "w": round(x1 - x0, 1), "h": round(y1 - y0, 1)}


def _rect_of(page: Any, step: dict[str, Any], vw: int, vh: int) -> dict[str, float]:
    if step["op"] == "column":
        raw = page.evaluate(_COLUMN_JS, [step["table"], step["col"]])
        if raw is None:
            raise CaptureError(f"no visible cells in column {step['col']} of {step['table']}")
    else:
        loc = page.locator(step["selector"]).first
        if loc.count() == 0:
            raise CaptureError(f"no element for {step['selector']}")
        box = loc.bounding_box()
        if box is None:
            raise CaptureError(f"{step['selector']} has no box")
        raw = {"x": box["x"], "y": box["y"], "w": box["width"], "h": box["height"]}
    rect = clip_rect(raw, vw, vh)
    if rect is None:
        raise CaptureError(f"{step.get('selector') or step.get('table')} is outside the viewport")
    return rect


def resolve_placeholders(route: str, base: str) -> str:
    """Fill ``{judged_run}`` and ``{group}`` from the corpus the server holds."""
    if "{judged_run}" in route:
        with urllib.request.urlopen(f"{base}/api/runs?limit=2000", timeout=30) as r:
            payload = json.loads(r.read())
        rows = payload["runs"] if isinstance(payload, dict) else payload
        # Deterministic: the first judged, passing, finished run by id.
        picks = sorted(
            (x for x in rows if x.get("status") == "finished" and x.get("passes")
             and x.get("judge_state") == "judged" and not x.get("dry_run")),
            key=lambda x: x["run_id"])
        if not picks:
            raise CaptureError("the corpus has no judged, passing, finished run")
        route = route.replace("{judged_run}", picks[0]["run_id"])
    if "{group}" in route:
        with urllib.request.urlopen(f"{base}/api/pairings", timeout=30) as r:
            default = json.loads(r.read()).get("default_group")
        if not default:
            raise CaptureError("the corpus has no default group")
        route = route.replace("{group}", default)
    return route


def record_beat(browser: Any, base: str, cfg: dict[str, Any], beat: dict[str, Any],
                out: Path, *, dry_run: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drive one beat. Returns (events, beat summary). Times come from the plan."""
    import time

    vw, vh = cfg["viewport"]["width"], cfg["viewport"]["height"]
    video = cfg["video"]
    kw: dict[str, Any] = {
        "viewport": {"width": vw, "height": vh}, "device_scale_factor": 1,
        "color_scheme": "light", "locale": "en-US", "timezone_id": "UTC",
    }
    if not dry_run:
        raw_dir = out / "raw" / beat["id"]
        raw_dir.mkdir(parents=True, exist_ok=True)
        kw.update(record_video_dir=str(raw_dir), record_video_size={"width": video["width"], "height": video["height"]})
    ctx = browser.new_context(**kw)
    ctx.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{cfg['theme']}')}}catch(e){{}}")
    ctx.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.zoom = "
        f"'{cfg['zoom']}'; }});")
    started = time.monotonic()
    page = ctx.new_page()
    events: list[dict[str, Any]] = []
    summary = {"id": beat["id"], "kind": "capture", "video": None, "video_offset_ms": 0}
    try:
        page.goto(f"{base}/#{resolve_placeholders(beat['route'], base)}")
        page.wait_for_selector("#view[data-ready]", timeout=30000)
        if page.get_attribute("#view", "data-ready") == "error":
            raise CaptureError(f"{beat['route']} failed to load")
        page.wait_for_selector(beat["ready"], timeout=30000)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(400 if not dry_run else 0)  # the 120ms crossfade and first paint settle
        summary["video_offset_ms"] = round((time.monotonic() - started) * 1000)
        clock = 0
        for step in beat["steps"]:
            op = step["op"]
            if op == "hold":
                if not dry_run:
                    page.wait_for_timeout(step["ms"])
            elif op == "hover":
                page.locator(step["selector"]).first.hover()
            elif op == "scroll":
                page.evaluate(_SCROLL_JS, [step["to"], 0 if dry_run else step["ms"], step.get("block", "start")])
                events.append({"t_ms": clock, "beat": beat["id"], "type": "scroll",
                               "label": f"scroll to {step['to']}",
                               "rect": _rect_of(page, {"op": "target", "selector": step["to"]}, vw, vh)})
            elif op in ("target", "column"):
                events.append({"t_ms": clock, "beat": beat["id"], "type": "target",
                               "label": step["label"], "rect": _rect_of(page, step, vw, vh)})
            clock += step_ms(step)
    finally:
        video_obj = page.video
        ctx.close()
        if not dry_run and video_obj is not None:
            dest = out / f"{beat['id']}.webm"
            shutil.move(video_obj.path(), dest)
            summary["video"] = dest.name
            shutil.rmtree(out / "raw" / beat["id"], ignore_errors=True)
    return events, summary


def _corpus_module():
    spec = importlib.util.spec_from_file_location("build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def capture(out: Path, *, dry_run: bool = False, only: list[str] | None = None,
            runs_dir: Path | None = None, beats_path: Path = BEATS) -> dict[str, Any]:
    """Run the kit and return the ``events.json`` document (also written to ``out``)."""
    refuse_if_keys()
    cfg = load_beats(beats_path)
    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(ROOT))
    from orchestral.web.server import Observatory, make_handler

    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    try:
        if runs_dir is None:
            runs_dir = tmp / "runs"
            with patch.dict(os.environ, {}, clear=True):
                _corpus_module().build_corpus(runs_dir, "full")
        obs = Observatory(runs_dir, FIXTURES / "tasks", FIXTURES / "models")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        events: list[dict[str, Any]] = []
        beats_out: list[dict[str, Any]] = []
        try:
            with sync_playwright() as pw:
                # Chromium gets an empty environment: no keys, no profile, no proxies.
                browser = pw.chromium.launch(env={"HOME": str(tmp), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
                for beat in cfg["beats"]:
                    entry = {"id": beat["id"], "kind": beat["kind"], "caption": beat["caption"],
                             "start_ms": round(beat["start_s"] * 1000), "end_ms": round(beat["end_s"] * 1000),
                             "video": None, "video_offset_ms": 0}
                    if beat["kind"] == "capture" and (not only or beat["id"] in only):
                        ev, summary = record_beat(browser, base, cfg, beat, out, dry_run=dry_run)
                        events += ev
                        entry["video"], entry["video_offset_ms"] = summary["video"], summary["video_offset_ms"]
                    beats_out.append(entry)
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out / "raw", ignore_errors=True)
    doc = {"version": SCHEMA_VERSION, "dry_run": dry_run, "theme": cfg["theme"],
           "viewport": cfg["viewport"], "zoom": cfg["zoom"], "video": cfg["video"],
           "beats": beats_out, "events": events}
    problems = validate_events(doc)
    if problems:
        raise CaptureError("events.json failed its own schema: " + "; ".join(problems))
    (out / "events.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="output directory (outside the repo)")
    ap.add_argument("--dry-run", action="store_true", help="drive every beat without video or waiting")
    ap.add_argument("--beat", action="append", help="record only this beat id (repeatable)")
    ap.add_argument("--runs-dir", type=Path, help="use an existing runs tree instead of the fixture corpus")
    args = ap.parse_args(argv)
    try:
        doc = capture(args.out, dry_run=args.dry_run, only=args.beat, runs_dir=args.runs_dir)
    except CaptureError as exc:
        print(f"observatory capture: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f"observatory capture: {exc}. Install the extra: pip install -e '.[shots]' "
              "and run: python -m playwright install chromium", file=sys.stderr)
        return 2
    videos = [b["video"] for b in doc["beats"] if b["video"]]
    print(f"wrote {args.out / 'events.json'} ({len(doc['events'])} events, {len(videos)} video(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
