"""http.server app for the web observatory.

All reads go through ``web.state`` (RunStore + run-dir files); all writes
are two POST routes that delegate to ``JobRegistry``. GET never mutates.
The handler holds no state of its own — per-request work is small, so the
stdlib ThreadingHTTPServer is sufficient.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from orchestral.storage import RunStore
from orchestral.web import render, state


class Observatory:
    """Bundles the server's dependencies so the handler stays thin."""

    def __init__(self, runs_dir: Path, tasks_dir: Path, models_dir: Path):
        self.store = RunStore(runs_dir)
        self.registry = state.JobRegistry(runs_dir, tasks_dir, models_dir, self.store)
        self.tasks_dir = Path(tasks_dir)
        self.models_dir = Path(models_dir)

    def run_dir(self, run_id: str) -> Path | None:
        meta = self.store.get_run(run_id)
        if meta is None:
            return None
        return Path(meta.run_dir)


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
            self._send(render.render_not_found(what), 404)

        # -- GET ----------------------------------------------------------

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path, qs = url.path, parse_qs(url.query)
            try:
                self._route_get(path, qs)
            except Exception as exc:  # never leak a stacktrace to the browser
                self._send(render.render_bad_request(f"internal error: {exc}"), 500)

        def _route_get(self, path: str, qs: dict[str, list[str]]) -> None:
            if path == "/":
                self._send(render.render_overview(state.overview_payload(obs.store, obs.registry)))
            elif path == "/runs":
                query = (qs.get("q") or [""])[0]
                rows = [r.to_dict() for r in state.history_rows(obs.store, query)]
                self._send(render.render_history(rows, query))
            elif path == "/leaderboard":
                sort = (qs.get("sort") or ["cost_per_pass"])[0]
                self._send(render.render_leaderboard(state.leaderboard_rows(obs.store, sort), sort))
            elif path == "/new":
                self._send(self._new_form())
            elif path == "/api/overview":
                self._json(state.overview_payload(obs.store, obs.registry))
            elif path.startswith("/api/run/"):
                self._api_run(path, qs)
            elif path.startswith("/run/"):
                self._run_get(path, qs)
            else:
                self._not_found(path)

        def _run_get(self, path: str, qs: dict[str, list[str]]) -> None:
            parts = path.strip("/").split("/")  # run/<id>[/<sub>]
            if len(parts) < 2:
                return self._not_found(path)
            run_id = parts[1]
            run_dir = obs.run_dir(run_id)
            meta = obs.store.get_run(run_id)
            if len(parts) == 3 and parts[2] == "live":
                if run_dir is None:
                    return self._not_found(f"run {run_id}")
                payload = state.live_payload(
                    run_dir, 0, started_at=meta.started_at if meta else None, meta=meta
                )
                job = obs.registry.job_for_run(run_id)
                self._send(render.render_live(
                    run_id, payload, cancellable=bool(job and job.active),
                ))
            elif len(parts) == 2:
                if run_dir is None:
                    return self._not_found(f"run {run_id}")
                tab = (qs.get("tab") or ["events"])[0]
                self._send(render.render_detail(
                    run_id, meta, obs.store.calls_for_run(run_id),
                    state.run_sections(run_dir), tab,
                ))
            else:
                self._not_found(path)

        def _api_run(self, path: str, qs: dict[str, list[str]]) -> None:
            parts = path.strip("/").split("/")  # api/run/<id>/live
            if len(parts) != 4 or parts[3] != "live":
                return self._not_found(path)
            run_id = parts[2]
            run_dir = obs.run_dir(run_id)
            if run_dir is None:
                return self._json({"error": f"unknown run {run_id}"}, 404)
            meta = obs.store.get_run(run_id)
            try:
                after = int((qs.get("after") or ["0"])[0])
            except ValueError:
                after = 0
            self._json(state.live_payload(
                run_dir, after, started_at=meta.started_at if meta else None, meta=meta,
            ))

        def _new_form(self, error: str = "") -> str:
            return render.render_new(
                state.task_choices(obs.tasks_dir),
                state.model_choices(obs.models_dir, "orchestrator"),
                state.model_choices(obs.models_dir, "worker"),
                state.model_choices(obs.models_dir, None),
                error,
            )

        # -- POST ---------------------------------------------------------

        def do_POST(self) -> None:
            url = urlparse(self.path)
            try:
                self._route_post(url.path)
            except Exception as exc:
                self._send(render.render_bad_request(f"internal error: {exc}"), 500)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 64 * 1024))  # forms are small; cap hard
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items()}

        def _route_post(self, path: str) -> None:
            if path == "/run":
                self._post_run()
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "run" and parts[2] == "cancel":
                run_id = parts[1]
                obs.registry.cancel_run(run_id)
                self._redirect(f"/run/{run_id}/live")
                return
            self._not_found(path)

        def _post_run(self) -> None:
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
                return self._send(self._new_form(f"unknown fields: {sorted(unknown)}"), 400)
            try:
                job = obs.registry.launch(spec)
            except ValueError as exc:
                return self._send(self._new_form(str(exc)), 400)
            # The run dir exists once on_run_created fires; poll briefly so the
            # redirect can land on the live view instead of a bare 303 to /.
            run_id = self._wait_run_id(job)
            self._redirect(f"/run/{run_id}/live" if run_id else "/")

        @staticmethod
        def _wait_run_id(job: state.Job, timeout: float = 5.0) -> str | None:
            """Brief wait for on_run_created so the redirect can target /live."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if job.run_ids:
                    return job.run_ids[-1]
                if not job.active:
                    return None  # setup failed or cancelled before a run existed
                time.sleep(0.05)
            return None

    return Handler


def serve(runs_dir: Path, tasks_dir: Path, models_dir: Path, port: int) -> None:
    obs = Observatory(Path(runs_dir), Path(tasks_dir), Path(models_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(obs))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
