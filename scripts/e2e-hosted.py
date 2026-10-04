#!/usr/bin/env python3
"""Local end-to-end of the hosted observatory: Worker + R2 + SPA, no Cloudflare.

    python3 scripts/e2e-hosted.py --shots ~/.local/share/design-program/orch-hosted

1. builds the key-free U23 fixture corpus in a temp dir (holdout runs get a canary
   in their files);
2. renders what ``harness.py sync --push`` would send (state chunks and per-run
   bodies) by capturing ``cf._post``, so the e2e exercises the real sync code;
3. runs ``wrangler dev --local`` (Miniflare) on ``wrangler.e2e.toml`` and seeds its
   local R2 bucket through the test-only ``/__seed`` route;
4. drives the hosted SPA with Playwright through every route and asserts: no /api
   error responses, no console errors, no write UI, no POSTs, the holdout run is
   withheld and the canary appears in no response.

Needs the browser venv (``BROWSER=1 scripts/bootstrap-venv.sh``) and node. Never
logs in to or contacts Cloudflare (``--local``; telemetry off).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestral import cf  # noqa: E402
from orchestral.storage import RunStore  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "observatory"
WORKER = REPO / "infra" / "cloudflare" / "observatory"
HOST = "obs.shippedit.dev"
CANARY = "CANARY-HOSTED-E2E-3f8a1c"


def _corpus():
    spec = importlib.util.spec_from_file_location("bfc", REPO / "scripts" / "build-fixture-corpus.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_bodies(work: Path) -> tuple[list[tuple[str, dict]], dict]:
    """Corpus + the exact ingest bodies cf.sync would POST."""
    root = work / "repo"
    shutil.copytree(FIXTURES / "tasks", root / "tasks")
    shutil.copytree(FIXTURES / "models", root / "models")
    (root / "experiments").mkdir()
    shutil.copy(REPO / "experiments" / "jev-ab.yaml", root / "experiments" / "jev-ab.yaml")
    with patch.dict(os.environ, {}, clear=True):
        manifest = _corpus().build_corpus(work / "runs", "full")
    store = RunStore(work / "runs")
    held = [r for r in store.list_runs(limit=None) if (r.config or {}).get("holdout")]
    for r in held:
        (Path(r.run_dir) / "artifact.html").write_text(f"<p>{CANARY}</p>")
        (Path(r.run_dir) / "plan.md").write_text(CANARY)
    manifest["holdout_run_id"] = held[0].run_id
    held_ids = {r.run_id for r in held}
    shown = next(r for r in store.list_runs(limit=None)
                 if r.status == "finished" and r.run_id not in held_ids and r.run_dir)
    (Path(shown.run_dir) / "artifact.html").write_text("<!doctype html><h1>artifact-ok</h1>")
    manifest["artifact_run_id"] = shown.run_id
    sent: list[tuple[str, dict]] = []

    def fake_post(_client, path, body):
        sent.append((path, body))
        return {"ok": True, "written": len(body.get("payloads", {}))}

    env = {"ORCHESTRAL_CF_ID": "e2e", "ORCHESTRAL_CF_SECRET": "e2e"}
    with patch.object(cf, "_post", fake_post), patch.dict(os.environ, env):
        result = cf.sync(store, root / "tasks", root / "models", root / "groups.yaml",
                         push=True, all_runs=True)
    if result.errors:
        raise SystemExit(f"sync errors: {result.errors[:3]}")
    return sent, manifest


def to_objects(sent: list[tuple[str, dict]]) -> dict[str, dict]:
    objs: dict[str, dict] = {}
    for _path, body in sent:
        for key, payload in body.get("payloads", {}).items():
            objs[key] = {"json": payload}
        for key, b64 in body.get("files", {}).items():
            objs[key] = {"b64": b64}
    return objs


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# wrangler dev serves https with a throwaway certificate: .dev is HSTS-preloaded, so
# Chromium refuses plain http for obs.shippedit.dev.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Host": HOST})
    with urllib.request.urlopen(req, timeout=120, context=_TLS) as r:
        return json.load(r)


def start_worker(port: int, persist: Path, log: Path) -> subprocess.Popen:
    env = {**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "1",
           "WRANGLER_LOG": "warn", "NO_COLOR": "1"}
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CF_API_TOKEN"):
        env.pop(k, None)
    proc = subprocess.Popen(
        ["npx", "--no-install", "wrangler", "dev", "--local", "--config", "wrangler.e2e.toml",
         "--port", str(port), "--ip", "127.0.0.1", "--local-protocol", "https", "--persist-to", str(persist)],
        cwd=WORKER, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"wrangler dev exited early:\n{log.read_text()[-2000:]}")
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://127.0.0.1:{port}/api/meta", headers={"Host": HOST}), timeout=3, context=_TLS)
            return proc
        except urllib.error.HTTPError:
            return proc  # 404 before seeding is still a live Worker
        except OSError:
            time.sleep(1)
    raise SystemExit("wrangler dev never came up")


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.checks: list[str] = []

    def ok(self, msg: str) -> None:
        self.checks.append(msg)
        print(f"  ok   {msg}")

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"  FAIL {msg}")


def _theme_pass(browser, theme: str, base: str, routes: dict[str, str], rid: str, held: str,
                artifact_run: tuple[str, str] | None, shots: Path | None, f: Findings) -> None:
    ctx = browser.new_context(viewport={"width": 1360, "height": 900}, color_scheme=theme,
                              ignore_https_errors=True)
    page = ctx.new_page()
    bad_api: list[str] = []
    console: list[str] = []
    posts: list[str] = []
    bodies: list[str] = []
    n_api = 0

    def on_response(r) -> None:
        nonlocal n_api
        if "/api/" in r.url:
            n_api += 1
            if r.status >= 400:
                bad_api.append(f"{r.status} {r.url}")
        if r.url.startswith(base) and "image" not in (r.headers.get("content-type") or ""):
            with contextlib.suppress(Exception):
                bodies.append(r.text())

    page.on("response", on_response)
    page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console.append(f"pageerror {e}"))
    page.on("request", lambda r: posts.append(f"{r.method} {r.url}") if r.method != "GET" else None)
    for name, route in routes.items():
        page.goto(base + route)
        page.wait_for_selector("#view[data-ready]", timeout=30000)
        state = page.get_attribute("#view", "data-ready")
        page.wait_for_timeout(400)
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / (f"{name}.png" if theme == "light" else f"{name}-dark.png")),
                            full_page=theme == "light")
        text = page.inner_text("#view")
        if name == "new-readonly":
            (f.ok if "read-only" in text else f.fail)(f"[{theme}] {name}: read-only state shown")
        elif state != "ok":
            f.fail(f"[{theme}] {name}: view state {state}: {text[:160]!r}")
        else:
            f.ok(f"[{theme}] {name}: rendered")
        if name == "run-holdout":
            (f.ok if "withheld" in text.lower() else f.fail)(f"[{theme}] holdout run page says it is withheld")
            chips = " ".join(page.locator("#view .chip").all_inner_texts())
            verdict = [w for w in ("Pass", "Fail") if re.search(rf"\b{w}\b", chips)]
            (f.ok if not verdict else f.fail)(f"[{theme}] holdout page shows no pass/fail verdict {verdict}")
            if CANARY in text:
                f.fail(f"[{theme}] holdout page shows canary")
    if "__V__" in page.content():
        f.fail(f"[{theme}] literal __V__ in the page")
    mode = page.evaluate("document.documentElement.dataset.mode")
    (f.ok if mode == "hosted" else f.fail)(f"[{theme}] mode={mode}")
    # read-only UI: no capability-gated controls, no write controls, no non-GET requests
    gated = page.locator("[data-needs]").count()
    (f.ok if gated == 0 else f.fail)(f"[{theme}] no capability-gated controls left ({gated})")
    page.goto(base + f"/#/run/{rid}")
    page.wait_for_selector("#view[data-ready='ok']")
    texts = " | ".join(page.locator("button, a.btn").all_inner_texts()).lower()
    writes = [w for w in ("launch", "cancel", "abandon", "flag", "download png", "start") if w in texts]
    (f.ok if not writes else f.fail)(f"[{theme}] no write buttons on a run page {writes}")
    (f.ok if not posts else f.fail)(f"[{theme}] no non-GET requests {posts[:3]}")
    (f.ok if not bad_api else f.fail)(f"[{theme}] {n_api} /api requests, none 4xx/5xx {bad_api[:5]}")
    (f.ok if not console else f.fail)(f"[{theme}] no console errors {console[:3]}")
    leaked = [i for i, t in enumerate(bodies) if CANARY in t]
    (f.ok if not leaked else f.fail)(f"[{theme}] canary absent from {len(bodies)} responses")
    # probes that deliberately provoke 4xx run after the no-4xx assertions above
    art = artifact_run
    if not art:
        f.fail(f"[{theme}] the corpus has no artifact to serve")
    else:
        probe = """async u => { const r = await fetch(u); return [r.status, r.headers.get('content-security-policy') || ''] }"""
        status, csp = page.evaluate(probe, f"/api/run/{art[0]}/artifact")
        (f.ok if status == 200 and "sandbox" in csp else f.fail)(
            f"[{theme}] artifact {art[0]} served ({status}) under CSP sandbox")
        bad = page.evaluate(probe, f"/api/run/{art[0]}/artifact/..%2Fx")[0]
        (f.ok if bad == 400 else f.fail)(f"[{theme}] traversal member is refused ({bad})")
        page.goto(f"{base}/#/run/{art[0]}")
        page.wait_for_selector("#view[data-ready='ok']")
        page.wait_for_selector("#view iframe.artifact-frame", timeout=10000)
        if shots:
            page.screenshot(path=str(shots / f"run-artifact{'' if theme == 'light' else '-dark'}.png"))
        held_status = page.evaluate(probe, f"/api/run/{held}/artifact")[0]
        (f.ok if held_status == 404 else f.fail)(f"[{theme}] holdout artifact is 404 ({held_status})")
    ctx.close()


def drive(port: int, manifest: dict, groups: list[str], artifact_run: tuple[str, str] | None,
          shots: Path | None) -> Findings:
    from urllib.parse import quote

    from playwright.sync_api import sync_playwright

    f = Findings()
    base = f"https://{HOST}:{port}"
    rid, held = manifest["failed_run_id"], manifest["holdout_run_id"]
    a, b = groups[0], groups[1]
    routes = {
        "now": "/", "runs": "/#/runs", "leaderboard": "/#/leaderboard",
        "compare": f"/#/compare?a={quote(a, safe='')}&b={quote(b, safe='')}",
        "experiment": "/#/experiment?matrix=jev-ab", "cards": "/#/cards",
        "card": f"/#/card?kind=group&target={quote(a, safe='')}", "models": "/#/models",
        "about": "/#/about", "run": f"/#/run/{rid}", "run-holdout": f"/#/run/{held}",
        "new-readonly": "/#/new",
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[f"--host-resolver-rules=MAP {HOST} 127.0.0.1"])
        for theme in ("light", "dark"):
            _theme_pass(browser, theme, base, routes, rid, held, artifact_run, shots, f)
        browser.close()
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ap.add_argument("--cache", type=Path, default=None,
                    help="reuse/write the rendered corpus objects (json) to skip the slow render")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="orch-e2e-"))
    proc = None
    try:
        if args.cache and args.cache.is_file():
            cached = json.loads(args.cache.read_text())
            objs, manifest = cached["objs"], cached["manifest"]
            print(f"reusing {len(objs)} cached objects")
        else:
            print("rendering corpus and sync bodies ...", flush=True)
            sent, manifest = render_bodies(work)
            objs = to_objects(sent)
            print(f"  {len(sent)} POSTs, {len(objs)} objects")
            if args.cache:
                args.cache.write_text(json.dumps({"objs": objs, "manifest": manifest}))
        port = free_port()
        subprocess.run([sys.executable, str(REPO / "scripts" / "build-hosted-assets.py")], check=True)
        proc = start_worker(port, work / "persist", work / "wrangler.log")
        keys = list(objs)
        for i in range(0, len(keys), 60):
            post_json(f"https://127.0.0.1:{port}/__seed", {"objects": {k: objs[k] for k in keys[i:i + 60]}})
        print(f"  seeded {len(keys)} objects into local R2")
        groups = [g["group"] for g in objs["api/groups.json"]["json"]]
        art = next((m for k in keys if (m := re.match(r"runs/([^/]+)/artifact\.(\w+)$", k))), None)
        findings = drive(port, manifest, [g for g in groups if "holdout" not in g],
                         (art.group(1), art.group(2)) if art else None, args.shots)
        print(f"\n{len(findings.checks)} checks passed, {len(findings.errors)} failed")
        return 1 if findings.errors else 0
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
