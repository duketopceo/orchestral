"""Deterministic validator for `terminal` tasks — virtual shell replay.

A terminal task ships a fixture filesystem and expected end state in
`metadata`:

- `fs`     — seed files: {path: content}
- `expect` — grading: {files: {path: {contains|equals|absent}}, max_commands?}

The worker produces a JSON command plan: [{"run": "cat app.ini"}, ...].
The harness seeds a tmpdir from `fs`, interprets each command in a
restricted virtual shell (cat, ls, echo>, touch, mkdir, cp, mv, rm, cd,
grep, sed -i s/x/y/), and grades the resulting filesystem against `expect`.

No real subprocess ever runs — this is a stub tier. Paths are confined to
the tmpdir; absolute paths and `..` escapes are rejected as command errors.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from orchestral.apistub import parse_plan

_SED_RE = re.compile(r"^sed\s+-i\s+['\"]?s/(.+?)/(.*?)/['\"]?\s+(\S+)\s*$")
_PREVIEW = 400
_MAX_COMMANDS = 200


def _resolve(root: Path, cwd: Path, arg: str) -> Path:
    """Confine `arg` to `root` — absolute paths and .. escapes are rejected."""
    p = Path(arg)
    target = (root / p.relative_to("/")) if p.is_absolute() else (cwd / p)
    resolved = target.resolve()
    if not (resolved == root or root in resolved.parents):
        raise ValueError(f"path escapes sandbox: {arg}")
    return resolved


def _split_redirect(tokens: list[str]) -> tuple[list[str], str | None, bool]:
    """Split `echo x > f` into ([echo, x], f, append?)."""
    for i, t in enumerate(tokens):
        if t in (">", ">>"):
            return tokens[:i], (tokens[i + 1] if i + 1 < len(tokens) else None), t == ">>"
    return tokens, None, False


def run_command(root: Path, cwd: Path, raw: str) -> tuple[Path, str]:
    """Execute one virtual-shell command. Returns (new cwd, output)."""
    raw = raw.strip()
    if not raw:
        return cwd, ""
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return cwd, f"parse error: {exc}"
    if not tokens:
        return cwd, ""
    try:
        return _exec(root, cwd, tokens, raw)
    except ValueError as exc:
        return cwd, f"error: {exc}"
    except OSError as exc:
        return cwd, f"error: {exc.strerror or exc}"


def _exec(root: Path, cwd: Path, tokens: list[str], raw: str) -> tuple[Path, str]:
    cmd, rest = tokens[0], tokens[1:]
    args, redirect, append = _split_redirect(rest)
    if redirect is not None:
        if cmd not in ("echo", "printf", "cat"):
            return cwd, f"error: redirect not supported for {cmd}"
        target = _resolve(root, cwd, redirect)
        if cmd == "cat":
            if not args:
                return cwd, "error: cat > needs a source"
            body = _resolve(root, cwd, args[0]).read_text()
        else:
            body = " ".join(args).replace("\\n", "\n") + ("\n" if cmd == "echo" else "")
        target.parent.mkdir(parents=True, exist_ok=True)
        if append and target.exists():
            body = target.read_text() + body
        target.write_text(body)
        return cwd, ""

    m = _SED_RE.match(raw)
    if m:
        pat, rep, fname = m.groups()
        path = _resolve(root, cwd, fname)
        if not path.is_file():
            return cwd, f"error: {fname}: no such file"
        path.write_text(re.sub(pat, rep, path.read_text()))
        return cwd, ""

    if cmd == "cd":
        dest = _resolve(root, cwd, args[0] if args else "/")
        if not dest.is_dir():
            return cwd, f"error: {args[0]}: not a directory"
        return dest, ""
    if cmd == "cat":
        path = _resolve(root, cwd, args[0])
        return cwd, path.read_text()
    if cmd == "ls":
        path = _resolve(root, cwd, args[0]) if args else cwd
        if not path.is_dir():
            return cwd, f"error: {args[0] if args else '.'}: not a directory"
        return cwd, "\n".join(sorted(p.name for p in path.iterdir()))
    if cmd == "pwd":
        rel = str(cwd.relative_to(root))
        return cwd, "/" if rel == "." else rel
    if cmd == "touch":
        path = _resolve(root, cwd, args[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return cwd, ""
    if cmd == "mkdir":
        flag = args[0] == "-p"
        path = _resolve(root, cwd, args[-1])
        path.mkdir(parents=flag, exist_ok=flag)
        return cwd, ""
    if cmd == "cp":
        src, dst = _resolve(root, cwd, args[0]), _resolve(root, cwd, args[1])
        if not src.is_file():
            return cwd, f"error: {args[0]}: no such file"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
        return cwd, ""
    if cmd == "mv":
        src, dst = _resolve(root, cwd, args[0]), _resolve(root, cwd, args[1])
        if not src.exists():
            return cwd, f"error: {args[0]}: no such file"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return cwd, ""
    if cmd == "rm":
        recursive = args[0] in ("-r", "-rf", "-f")
        path = _resolve(root, cwd, args[-1])
        if path.is_dir() and not recursive:
            return cwd, f"error: {args[-1]}: is a directory (use -r)"
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            return cwd, f"error: {args[-1]}: no such file"
        return cwd, ""
    if cmd == "grep":
        pat, fname = args[0], args[1]
        path = _resolve(root, cwd, fname)
        if not path.is_file():
            return cwd, f"error: {fname}: no such file"
        return cwd, "\n".join(line for line in path.read_text().splitlines() if re.search(pat, line))
    return cwd, f"error: unsupported command: {cmd}"


def check_terminal(metadata: dict[str, Any], plan_text: str) -> dict[str, Any]:
    """Replay `plan_text` in the virtual shell; grade final FS vs expect."""
    report: dict[str, Any] = {
        "parsed": False,
        "commands": None,
        "command_errors": [],
        "transcript": [],
        "files_checked": {},
        "missing": [],
        "unexpected_content": [],
        "score": None,
        "passes": False,
    }
    expect = metadata.get("expect") or {}
    want_files = expect.get("files") or {}
    if not isinstance(want_files, dict) or not want_files:
        report["command_errors"].append("terminal task has no metadata.expect.files")
        return report

    plan = parse_plan(plan_text)
    if plan is None:
        report["command_errors"].append("artifact does not contain a parseable JSON command plan")
        return report
    if len(plan) > _MAX_COMMANDS:
        report["command_errors"].append(f"plan exceeds {_MAX_COMMANDS} commands")
        return report
    report["parsed"] = True
    report["commands"] = len(plan)

    with tempfile.TemporaryDirectory(prefix="orchestral-term-") as tmp:
        root = Path(tmp).resolve()
        for rel, body in (metadata.get("fs") or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(body))
        cwd = root
        for i, call in enumerate(plan):
            raw = str(call.get("run") or "")
            cwd, output = run_command(root, cwd, raw)
            report["transcript"].append({"i": i, "run": raw, "output": output[:_PREVIEW]})
            if output.startswith(("error:", "parse error:")):
                report["command_errors"].append(f"command {i} ({raw[:60]}): {output[:120]}")

        matched = 0
        for rel, rule in want_files.items():
            rule = rule or {}
            path = root / rel
            if rule.get("absent"):
                ok = not path.exists()
            elif not path.is_file():
                ok = False
            elif "equals" in rule:
                ok = path.read_text() == str(rule["equals"])
            elif "contains" in rule:
                ok = str(rule["contains"]) in path.read_text()
            elif "matches" in rule:
                ok = bool(re.search(str(rule["matches"]), path.read_text()))
            else:
                ok = path.is_file()
            report["files_checked"][rel] = ok
            if ok:
                matched += 1
            else:
                report["missing"].append(rel)
    report["score"] = matched / len(want_files)
    max_cmds = expect.get("max_commands")
    over_budget = isinstance(max_cmds, int) and len(plan) > max_cmds
    report["over_command_budget"] = over_budget
    report["passes"] = matched == len(want_files) and not report["command_errors"] and not over_budget
    return report
