"""Deterministic validator for `api` tasks — local stub replay.

A api task ships a fake API contract in `metadata`:

- `stub`   — routes the local server answers: [{method, path, status, json}]
- `calls`  — the expected request plan:      [{method, path, json?, params?}]
- `strict` — optional, default true: unexpected calls also fail the run

The worker produces a JSON request plan (a list of call objects). The
harness starts a real `http.server` on 127.0.0.1, replays each call over
real HTTP, and the stub records what it actually received. Score is the
fraction of expected calls that arrived correctly; `passes` requires every
expected call and — under `strict` — no unexpected ones.

Everything is loopback-only: the stub binds 127.0.0.1 on an ephemeral port
and is shut down before the report returns.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_PREVIEW = 500
_CALL_TIMEOUT = 5.0


def parse_plan(text: str) -> list[dict[str, Any]] | None:
    """Pull a JSON call list out of worker output: fenced or raw."""
    raw = str(text or "").strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            plan = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(plan, dict):
        plan = plan.get("calls", plan.get("requests"))
    if not isinstance(plan, list) or not all(isinstance(c, dict) for c in plan):
        return None
    return plan


def _route_key(method: str, path: str) -> str:
    return f"{method.upper()} {path.split('?')[0].rstrip('/') or '/'}"


class _StubHandler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, dict[str, Any]]] = {}
    hits: ClassVar[list[dict[str, Any]]] = []

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = body.decode("utf-8", "replace")
        parsed = urllib.parse.urlsplit(self.path)
        hit = {
            "method": self.command,
            "path": parsed.path.rstrip("/") or "/",
            "params": {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(parsed.query).items()},
            "json": payload,
        }
        type(self).hits.append(hit)
        route = type(self).routes.get(_route_key(self.command, str(hit["path"])))
        status = int(route.get("status", 200)) if route else 404
        response = json.dumps(route.get("json") if route else {"error": "not stubbed"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle

    def log_message(self, *args: Any) -> None:
        pass


def run_stub(routes: list[dict[str, Any]]) -> tuple[ThreadingHTTPServer, list[dict[str, Any]]]:
    """Start a loopback stub for `routes`; return (server, shared hit log)."""
    table = {_route_key(str(r.get("method", "GET")), str(r.get("path", "/"))): r for r in routes}

    class Handler(_StubHandler):
        pass

    Handler.routes = table
    Handler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, Handler.hits


def replay_plan(base_url: str, plan: list[dict[str, Any]]) -> list[str]:
    """Send every call in `plan` to `base_url`; return per-call errors."""
    errors: list[str] = []
    for i, call in enumerate(plan):
        method = str(call.get("method", "GET")).upper()
        path = str(call.get("path", "/"))
        params = call.get("params")
        if isinstance(params, dict) and params:
            path += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        body = call.get("json")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_CALL_TIMEOUT):
                pass
        except urllib.error.HTTPError:
            pass  # stub 404s are still recorded as hits
        except OSError as exc:
            errors.append(f"call {i} ({method} {path}) failed: {exc}")
    return errors


def _call_matches(expected: dict[str, Any], hit: dict[str, Any]) -> bool:
    if _route_key(str(expected.get("method", "GET")), str(expected.get("path", "/"))) != (
        _route_key(hit["method"], hit["path"])
    ):
        return False
    if "json" in expected and expected["json"] != hit["json"]:
        return False
    if "params" in expected:
        want = {str(k): str(v) for k, v in expected["params"].items()}
        got = {str(k): str(v) for k, v in (hit.get("params") or {}).items()}
        if want != got:
            return False
    return True


def check_api(metadata: dict[str, Any], plan_text: str) -> dict[str, Any]:
    """Replay `plan_text` against the stub; returns a report dict."""
    report: dict[str, Any] = {
        "parsed": False,
        "calls_expected": None,
        "calls_made": None,
        "matched": 0,
        "missing": [],
        "unexpected": [],
        "errors": [],
        "score": None,
        "passes": False,
    }
    expected = metadata.get("calls")
    if not isinstance(expected, list) or not expected:
        report["errors"].append("api task has no metadata.calls")
        return report
    routes = metadata.get("stub") or []
    report["calls_expected"] = len(expected)

    plan = parse_plan(plan_text)
    if plan is None:
        report["errors"].append("artifact does not contain a parseable JSON call plan")
        return report
    report["parsed"] = True

    server, hits = run_stub([r for r in routes if isinstance(r, dict)])
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        report["errors"].extend(replay_plan(base, plan))
    finally:
        server.shutdown()
        server.server_close()
    report["calls_made"] = len(hits)

    remaining = list(hits)
    for want in expected:
        if not isinstance(want, dict):
            continue
        index = next(
            (i for i, hit in enumerate(remaining) if _call_matches(want, hit)), None
        )
        if index is None:
            report["missing"].append(_route_key(str(want.get("method", "GET")), str(want.get("path", "/"))))
        else:
            report["matched"] += 1
            remaining.pop(index)
    report["unexpected"] = [f"{h['method']} {h['path']}" for h in remaining]
    report["score"] = report["matched"] / len(expected)
    strict = bool(metadata.get("strict", True))
    report["passes"] = not report["missing"] and (not strict or not report["unexpected"])
    if report["missing"] or report["unexpected"]:
        report["hits_preview"] = json.dumps(hits)[:_PREVIEW]
    return report
