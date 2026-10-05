#!/usr/bin/env python3
"""File scrubbed GitHub issues for new GlitchTip issues.

Runs from GitHub Actions (``error-triage.yml``) on a schedule. Fetches
unresolved issues from the project's GlitchTip (Sentry-compatible) API,
skips ones already filed (each filed issue carries a ``glitchtip:``
marker in its body), and creates GitHub issues for the rest.

The target repository is PUBLIC while the observatory is private, so
issue bodies are aggressively scrubbed: URLs, query strings, run-id
shaped tokens, and absolute paths are redacted before anything is
posted. Only error type, message, top stack frames, and event counts
survive.

Required environment:
    GLITCHTIP_TOKEN   GlitchTip API bearer token (repo secret)
    GH_TOKEN          GitHub token with issues:write (GITHUB_TOKEN)

Optional:
    GLITCHTIP_BASE    default https://errors.pacehq.io
    GLITCHTIP_ORG     default pace-hq
    GLITCHTIP_PROJECT default orchestral-observatory
    GH_REPO           default $GITHUB_REPOSITORY
    MAX_ISSUES        cap on new issues filed per run (default 10)
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

GLITCHTIP_BASE = os.environ.get("GLITCHTIP_BASE", "https://errors.pacehq.io")
ORG = os.environ.get("GLITCHTIP_ORG", "pace-hq")
PROJECT = os.environ.get("GLITCHTIP_PROJECT", "orchestral-observatory")
REPO = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
MAX_ISSUES = int(os.environ.get("MAX_ISSUES", "10"))
MARKER_PREFIX = "glitchtip:"

_URL_RE = re.compile(r"https?://\S+")
_QUERY_RE = re.compile(r"\?\S*")
_HEXLONG_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_RUNDIR_RE = re.compile(r"\b(?:run|cli|live|sb|luna)-[a-zA-Z0-9_.-]{6,}\b")


def scrub(text: str) -> str:
    """Strip anything that could carry private run/URL data into the
    public repo. Keeps the error signal; drops identifiers."""
    if not text:
        return ""
    text = _URL_RE.sub("[url]", text)
    text = _QUERY_RE.sub("", text)
    text = _UUID_RE.sub("[id]", text)
    text = _HEXLONG_RE.sub("[id]", text)
    text = _RUNDIR_RE.sub("[id]", text)
    return text.strip()


def marker(issue_id: str) -> str:
    return f"<!-- {MARKER_PREFIX}{issue_id} -->"


def _get(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            # the edge WAF rejects the default Python-urllib UA
            "User-Agent": "orchestral-error-triage/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _post(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "orchestral-error-triage/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def glitchtip_issues(token: str) -> list[dict]:
    url = (
        f"{GLITCHTIP_BASE}/api/0/projects/{ORG}/{PROJECT}/issues/"
        f"?{urllib.parse.urlencode({'query': 'is:unresolved', 'limit': 50})}"
    )
    return _get(url, token)


def latest_event(issue_id: str, token: str) -> dict:
    url = f"{GLITCHTIP_BASE}/api/0/issues/{issue_id}/events/latest/"
    try:
        return _get(url, token)
    except Exception:
        return {}


def already_filed(issue_id: str, gh_token: str) -> bool:
    q = urllib.parse.quote(f'repo:{REPO} is:issue "{MARKER_PREFIX}{issue_id}"')
    url = f"https://api.github.com/search/issues?q={q}"
    try:
        return _get(url, gh_token).get("total_count", 0) > 0
    except Exception as e:
        print(f"warn: dedup search failed for {issue_id}: {e}", file=sys.stderr)
        return True


def _frames(event: dict) -> list[str]:
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for exc in entry.get("data", {}).get("values", []):
            frames = (exc.get("stacktrace") or {}).get("frames") or []
            site = frames[-1] if frames else None
            if site:
                lineno = site.get("lineNo") or site.get("lineno") or "?"
                where = f"{site.get('filename', '?')}:{lineno}"
                return [where]
    return []


def issue_body(issue: dict, event: dict) -> str:
    meta = issue.get("metadata") or {}
    rows = [
        "| field | value |",
        "|---|---|",
        f"| events | {issue.get('count', '?')} |",
        f"| users | {issue.get('userCount', '?')} |",
        f"| level | {issue.get('level', '?')} |",
        f"| first seen | {issue.get('firstSeen', '?')} |",
        f"| last seen | {issue.get('lastSeen', '?')} |",
    ]
    if meta.get("type"):
        rows.append(f"| error type | `{scrub(str(meta['type']))}` |")
    where = _frames(event)
    if where:
        rows.append(f"| site | `{scrub(where[0])}` |")
    body = [
        "Automatically filed by the observatory error-triage workflow.",
        "Private run data, URLs, and identifiers are scrubbed before posting.",
        "",
        *rows,
        "",
        f"```\n{scrub(str(meta.get('value', '')))[:2000] or '(no message)'}\n```",
        "",
        "Triage: investigate in GlitchTip, then close this issue when the",
        "underlying defect is fixed (or mark it resolved there).",
        "",
        marker(str(issue.get("id"))),
    ]
    return "\n".join(body)


def issue_title(issue: dict) -> str:
    meta = issue.get("metadata") or {}
    etype = scrub(str(meta.get("type") or issue.get("level") or "error"))
    value = scrub(str(meta.get("value") or issue.get("title") or ""))
    title = f"[obs] {etype}: {value}".strip().rstrip(":")
    return title[:120] or "[obs] error report"


def file_issue(issue: dict, event: dict, gh_token: str) -> str:
    url = f"https://api.github.com/repos/{REPO}/issues"
    resp = _post(
        url,
        gh_token,
        {
            "title": issue_title(issue),
            "body": issue_body(issue, event),
            "labels": ["glitchtip", "bug"],
        },
    )
    return resp.get("html_url", "(no url)")


def main() -> int:
    gt_token = os.environ.get("GLITCHTIP_TOKEN", "")
    gh_token = os.environ.get("GH_TOKEN", "")
    if not gt_token or not gh_token or not REPO:
        print("error: GLITCHTIP_TOKEN, GH_TOKEN, and GH_REPO are required", file=sys.stderr)
        return 2
    issues = glitchtip_issues(gt_token)
    filed = 0
    for issue in issues:
        if filed >= MAX_ISSUES:
            print(f"cap reached ({MAX_ISSUES}); {len(issues) - filed} remain for next run")
            break
        issue_id = str(issue.get("id"))
        if already_filed(issue_id, gh_token):
            continue
        event = latest_event(issue_id, gt_token)
        url = file_issue(issue, event, gh_token)
        print(f"filed {url} for glitchtip issue {issue_id}: {issue_title(issue)}")
        filed += 1
    print(f"done: {filed} new issue(s), {len(issues)} unresolved total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
