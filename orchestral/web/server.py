"""http.server app for the web observatory.

All reads go through ``web.state`` (RunStore + run-dir files); all writes
are POST routes that delegate to ``JobRegistry``. GET never mutates.
The handler holds no state of its own — per-request work is small, so the
stdlib ThreadingHTTPServer is sufficient.

The UI is a static single-page app in ``ui/`` (hash-routed, no build step):
``GET /`` and any unknown non-API path serve ``app.html``; ``/static/*``
serves the app's assets. ``/api/*`` is the JSON surface the SPA polls.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from orchestral.storage import RunStore
from orchestral.web import render, state

# Static SPA assets live in <repo>/ui — server.py is orchestral/web/server.py.
UI_DIR = Path(__file__).resolve().parents[2] / "ui"

_ARTIFACT_TYPES = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "json": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


class Observatory:
    """Bundles the server's dependencies so the handler stays thin."""

    def __init__(self, runs_dir: Path, tasks_dir: Path, models_dir: Path,
                 allow_agent_exec: bool = False):
        self.store = RunStore(runs_dir)
        self.registry = state.JobRegistry(
            runs_dir, tasks_dir, models_dir, self.store,
            allow_agent_exec=allow_agent_exec,
        )
        self.tasks_dir = Path(tasks_dir)
        self.models_dir = Path(models_dir)
        self.groups_file = Path(tasks_dir).parent / "groups.yaml"

    def run_dir(self, run_id: str) -> Path | None:
        meta = self.store.get_run(run_id)
        if meta is None:
            return None
        return Path(meta.run_dir)


def _provider_ready(slug: str) -> bool:
    """True when the slug's provider is configured and its API-key env var is
    set — lets POST /api/thread degrade to templates instead of 500ing."""
    from orchestral.config import ModelConfig
    from orchestral.providers import provider_key

    _, base_url, env = provider_key(ModelConfig(
        slug=slug, name=slug, role="writer",
        input_price_per_mtok=0.0, output_price_per_mtok=0.0,
    ))
    return bool(base_url and env and os.environ.get(env))


def _safe_member(name: str) -> str | None:
    """Reject zip members that would escape the archive (../, absolute)."""
    if not name or name.startswith("/") or ".." in Path(name).parts:
        return None
    return name


def make_handler(obs: Observatory) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        server_version = "orchestral-observatory"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # quiet by default; the observatory's own logs live in runs/

        # -- helpers ------------------------------------------------------

        def _send(self, body: str | bytes, status: int = 200,
                  content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(json.dumps(payload, default=str), status, "application/json")

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _not_found(self, what: str) -> None:
            if what.startswith("/api/"):
                self._json({"error": f"not found: {what}"}, 404)
            else:
                self._send(render.render_not_found(what), 404)

        def _spa(self) -> None:
            """The single-page app shell — hash routing means every page
            path lands here and the client decides what to render."""
            app = UI_DIR / "app.html"
            if app.exists():
                self._send(app.read_bytes())
            else:
                self._send(render.render_bad_request("ui/app.html missing"), 500)

        def _static(self, path: str) -> None:
            name = path.removeprefix("/static/")
            if ".." in Path(name).parts or name.startswith("/"):
                return self._not_found(path)
            f = UI_DIR / name
            if not f.is_file():
                return self._not_found(path)
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(f.read_bytes(), 200, ctype)

        # -- GET ----------------------------------------------------------

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path, qs = url.path, parse_qs(url.query)
            try:
                self._route_get(path, qs)
            except Exception as exc:  # never leak a stacktrace to the browser
                if path.startswith("/api/"):
                    self._json({"error": str(exc)[:200]}, 500)
                else:
                    self._send(render.render_bad_request(f"internal error: {exc}"), 500)

        def _route_get(self, path: str, qs: dict[str, list[str]]) -> None:
            if path.startswith("/api/"):
                self._api_get(path, qs)
            elif path.startswith("/static/"):
                self._static(path)
            elif path == "/":
                self._spa()
            elif path in ("/runs", "/leaderboard", "/compare", "/new"):
                # legacy bookmarks → hash equivalents (hash isn't sent to
                # the server, so the SPA itself must own the target path)
                self._redirect(f"/#{path}")
            elif path.startswith("/run/"):
                parts = path.strip("/").split("/")
                if len(parts) >= 2 and obs.store.get_run(parts[1]) is not None:
                    self._redirect(f"/#/run/{parts[1]}")
                else:
                    self._not_found(path)
            else:
                self._not_found(path)

        @staticmethod
        def _q1(qs: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
            vals = qs.get(key)
            return vals[0] if vals else default

        def _api_get(self, path: str, qs: dict[str, list[str]]) -> None:
            if path == "/api/overview":
                self._json(state.overview_payload(obs.store, obs.registry))
            elif path == "/api/runs":
                self._json(state.runs_payload(
                    obs.store,
                    group=self._q1(qs, "group"),
                    task=self._q1(qs, "task"),
                    status=self._q1(qs, "status"),
                    q=self._q1(qs, "q", "") or "",
                    tasks_dir=obs.tasks_dir,
                ))
            elif path == "/api/groups":
                self._json(state.groups_payload(obs.store, obs.groups_file))
            elif path == "/api/compare":
                a, b = self._q1(qs, "a", "") or "", self._q1(qs, "b", "") or ""
                if not a or not b:
                    return self._json({"error": "compare needs ?a=<group>&b=<group>"}, 400)
                self._json(state.compare_payload(obs.store, a, b))
            elif path == "/api/flags":
                self._json(obs.store.annotations())
            elif path == "/api/card":
                kind = self._q1(qs, "kind", "group") or "group"
                target = self._q1(qs, "target", "") or ""
                payload = state.card_payload(
                    obs.store, kind, target,
                    group=self._q1(qs, "group"), tasks_dir=obs.tasks_dir,
                    groups_file=obs.groups_file,
                )
                if payload is None:
                    return self._json({"error": f"no {kind} '{target}'"}, 404)
                self._json(payload)
            elif path == "/api/pairings":
                self._json(state.pairings_payload(
                    obs.store, tasks_dir=obs.tasks_dir,
                    group=self._q1(qs, "group"),
                ))
            elif path == "/api/leaderboard":
                self._json(state.leaderboard_rows(obs.store, self._q1(qs, "sort", "cost_per_pass") or "cost_per_pass"))
            elif path == "/api/shot.png":
                self._shot_png(qs)
            elif path == "/api/tasks":
                self._json(state.task_choices(obs.tasks_dir))
            elif path == "/api/models":
                self._json(state.model_choices(obs.models_dir, self._q1(qs, "role")))
            elif path.startswith("/api/run/"):
                self._api_run(path, qs)
            else:
                self._json({"error": f"not found: {path}"}, 404)

        def _shot_png(self, qs: dict[str, list[str]]) -> None:
            """X-ready PNG of an SPA view — playwright screenshots the live
            page this same server is hosting. Cards capture just the
            ``.xcard`` node; other routes capture the settled ``#view``.
            """
            route = self._q1(qs, "route", "/") or "/"
            if not route.startswith("/") or route.startswith("//"):
                return self._json({"error": "route must be an app path like /card?kind=..."}, 400)
            try:
                from orchestral.shots import ScreenshotUnavailable, capture_page
                element = ".xcard" if route.startswith("/card") else None
                png = capture_page(
                    f"http://127.0.0.1:{self.server.server_port}/#{route}",
                    element=element,
                )
            except ScreenshotUnavailable as exc:
                return self._json({"error": str(exc)}, 503)
            name = "orchestral-" + re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-") + ".png"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def _api_run(self, path: str, qs: dict[str, list[str]]) -> None:
            parts = path.strip("/").split("/")  # api/run/<id>[/<sub>[/<member>]]
            if len(parts) < 3:
                return self._json({"error": f"not found: {path}"}, 404)
            run_id = parts[2]
            run_dir = obs.run_dir(run_id)
            if run_dir is None:
                return self._json({"error": f"unknown run {run_id}"}, 404)
            meta = obs.store.get_run(run_id)

            if len(parts) == 3:
                payload = state.run_detail_payload(
                    obs.store, run_id,
                    tasks_dir=obs.tasks_dir, groups_file=obs.groups_file,
                )
                if payload is None:
                    return self._json({"error": f"unknown run {run_id}"}, 404)
                payload["cancellable"] = bool(
                    (j := obs.registry.job_for_run(run_id)) and j.active
                )
                return self._json(payload)
            if parts[3] == "live":
                try:
                    after = int((qs.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                payload = state.live_payload(
                    run_dir, after, started_at=meta.started_at if meta else None, meta=meta,
                )
                job = obs.registry.job_for_run(run_id)
                payload["cancellable"] = bool(job and job.active)
                return self._json(payload)
            if parts[3] == "artifact":
                member = "/".join(parts[4:]) if len(parts) > 4 else None
                return self._artifact(run_dir, member)
            self._json({"error": f"not found: {path}"}, 404)

        def _artifact(self, run_dir: Path, member: str | None) -> None:
            """Serve artifact bytes. Top-level file by extension; a member
            path reads that file out of artifact.zip. Model output is treated
            as display content — the SPA sandboxes it in an iframe."""
            artifacts = sorted(run_dir.glob("artifact.*"))
            if not artifacts:
                return self._json({"error": "no artifact"}, 404)
            p = artifacts[0]
            if member is not None:
                safe = _safe_member(member)
                if safe is None or p.suffix != ".zip":
                    return self._json({"error": "bad member"}, 400)
                try:
                    with zipfile.ZipFile(p) as zf:
                        body = zf.read(safe)
                except (KeyError, zipfile.BadZipFile):
                    return self._json({"error": f"no member {safe}"}, 404)
                ctype = _ARTIFACT_TYPES.get(safe.rsplit(".", 1)[-1].lower(), "text/plain; charset=utf-8")
                return self._send(body, 200, ctype)
            ctype = _ARTIFACT_TYPES.get(p.suffix.lstrip(".").lower(), "application/octet-stream")
            self._send(p.read_bytes(), 200, ctype)

        # -- POST ---------------------------------------------------------

        _LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

        def _same_origin(self) -> bool:
            """CSRF guard for the unauthenticated POST surface.

            A cross-site form post carries the attacker's Origin — require a
            local Host (a foreign Host smells like DNS rebinding) and, when
            Origin is present, a local origin on this server's port. A
            missing Origin means a non-browser client, which can't be driven
            cross-origin by a page the user visits.
            """
            host = self.headers.get("Host")
            if not host or host.split(":", 1)[0].lower() not in self._LOCAL_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            o = urlparse(origin)
            if o.scheme not in ("http", "https") or (o.hostname or "").lower() not in self._LOCAL_HOSTS:
                return False
            port = o.port or (443 if o.scheme == "https" else 80)
            return port == self.server.server_port

        def do_POST(self) -> None:
            url = urlparse(self.path)
            try:
                if not self._same_origin():
                    return self._json({"error": "cross-origin POST rejected"}, 403)
                self._route_post(url.path)
            except Exception as exc:
                self._json({"error": str(exc)[:200]}, 500)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 64 * 1024))  # forms are small; cap hard
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items()}

        def _route_post(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if path in ("/run", "/api/run"):
                return self._post_run(json_out=path.startswith("/api/"))
            if len(parts) == 3 and parts[2] == "cancel" and parts[0] == "run":
                obs.registry.cancel_run(parts[1])
                return self._redirect(f"/#/run/{parts[1]}")
            if len(parts) == 4 and parts[3] == "cancel" and parts[0] == "api" and parts[1] == "run":
                cancelled = obs.registry.cancel_run(parts[2])
                return self._json({"cancelled": cancelled})
            if path == "/api/flag":
                form = self._form()
                try:
                    return self._json(obs.store.set_annotation(
                        form.get("kind", ""), form.get("target", ""),
                        form.get("flag", ""), form.get("note", ""),
                    ))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if path == "/api/thread":
                return self._post_thread()
            self._json({"error": f"not found: {path}"}, 404)

        def _post_thread(self) -> None:
            """Draft X follow-up posts for a card. Uses the writer model when
            configured; falls back to honest templates so the button never
            dead-ends on a missing key."""
            from orchestral.config import ModelConfig
            from orchestral.judge import draft_thread
            from orchestral.providers import provider_for

            form = self._form()
            kind = form.get("kind", "group")
            target = form.get("target", "")
            card = state.card_payload(
                obs.store, kind, target,
                group=form.get("group") or None, tasks_dir=obs.tasks_dir,
                groups_file=obs.groups_file,
            )
            if card is None:
                return self._json({"error": f"no {kind} '{target}'"}, 404)
            try:
                n = max(1, min(4, int(form.get("n", "3"))))
            except ValueError:
                n = 3
            writer = form.get("model") or ""
            client = model = None
            if writer and _provider_ready(writer):
                model = ModelConfig(slug=writer, name=writer, role="writer",
                                    input_price_per_mtok=0.0, output_price_per_mtok=0.0)
                client = provider_for(model)
            try:
                out = draft_thread(card=card, client=client, model=model, n=n)
            finally:
                if client is not None:
                    client.close()
            return self._json(out)

        def _post_run(self, json_out: bool) -> None:
            form = self._form()
            spec = {
                "task": form.get("task", ""),
                "orchestrator": form.get("orchestrator", ""),
                "worker": form.get("worker", ""),
                "judge": form.get("judge") or None,
                "replicates": form.get("replicates") or "1",
                "seed": form.get("seed") or None,
                "dry_run": form.get("dry_run") == "1",
            }
            unknown = set(form) - state.LAUNCH_FIELDS
            if unknown:
                return self._json({"error": f"unknown fields: {sorted(unknown)}"}, 400)
            try:
                job = obs.registry.launch(spec)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            # The run dir exists once on_run_created fires; poll briefly so
            # the response can point at the live view instead of nothing.
            run_id = self._wait_run_id(job)
            if json_out:
                self._json({"run_id": run_id, "label": job.label, "status": str(job.status)})
            else:
                self._redirect(f"/#/run/{run_id}" if run_id else "/")

        @staticmethod
        def _wait_run_id(job: state.Job, timeout: float = 5.0) -> str | None:
            """Brief wait for on_run_created so the response can name a run."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if job.run_ids:
                    return job.run_ids[-1]
                if not job.active:
                    return None  # setup failed or cancelled before a run existed
                time.sleep(0.05)
            return None

    return Handler


def serve(runs_dir: Path, tasks_dir: Path, models_dir: Path, port: int,
          allow_agent_exec: bool = False) -> None:
    obs = Observatory(Path(runs_dir), Path(tasks_dir), Path(models_dir),
                      allow_agent_exec=allow_agent_exec)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(obs))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
