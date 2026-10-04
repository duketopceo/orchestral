"""U5: copy lint (KTD11). User-visible strings carry no em dash, en dash or emoji,
and none of the banned product words. Code comments and docstrings are the only
allowlisted place; everything else a reader can see is linted."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# em dash, en dash, the symbol/dingbat blocks (warning sign, skull, check marks,
# stars), pictographs, and the emoji variation selector.
DASH_EMOJI = re.compile("[—–☀-➿⭐⭕️\U0001F000-\U0001FAFF]")
# DESIGN.md section 10: never "AI", never seamless/elevate/unlock.
BANNED_WORDS = re.compile(
    r"\bAI\b|(?i:\bseamless(?:ly)?\b|\belevat(?:e|es|ed|ing)\b|\bunlock(?:s|ed|ing)?\b)")

PY_TARGETS = sorted(
    [*(ROOT / "orchestral" / "web").glob("*.py"), *(ROOT / "orchestral" / "tui").glob("*.py"),
     ROOT / "orchestral" / "reporter.py", ROOT / "harness.py"])
UI_TARGETS = sorted(p for p in (ROOT / "ui").rglob("*") if p.suffix in {".js", ".mjs", ".html", ".css"})


def lint_text(text: str) -> list[str]:
    hits = [f"banned char {m.group()!r}" for m in DASH_EMOJI.finditer(text)]
    hits += [f"banned word {m.group()!r}" for m in BANNED_WORDS.finditer(text)]
    return hits


def python_string_violations(source: str) -> list[tuple[int, str]]:
    """Non-docstring string constants (f-string parts included) that break the rules.
    Comments never reach the AST, docstrings are skipped: the allowlist."""
    tree = ast.parse(source)
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)
    }
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            out += [(n.lineno, h) for h in lint_text(n.value)]
    return out


_REGEX_PREV = set("(,=:[!&|?{};") | {""}


def strip_js_comments(src: str) -> str:
    """Blank `//` and `/* */` comments, honouring strings, nested template
    literals and regex literals. Raises if it ends mid-literal (a misparse)."""
    out: list[str] = []
    i, n = 0, len(src)
    stack: list[str] = ["code"]  # "code" | "tpl" | "expr" (code inside ${ })
    depth: list[int] = []
    last_sig = ""
    while i < n:
        ch, mode = src[i], stack[-1]
        nxt = src[i + 1] if i + 1 < n else ""
        if mode == "tpl":
            if ch == "\\":
                out.append(src[i:i + 2])
                i += 2
                continue
            if ch == "`":
                stack.pop()
                out.append(ch)
                i += 1
                last_sig = "`"
                continue
            if ch == "$" and nxt == "{":
                stack.append("expr")
                depth.append(0)
                out.append("${")
                i += 2
                last_sig = "{"
                continue
            out.append(ch)
            i += 1
            continue
        # code / expr
        if ch == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                raise ValueError("unterminated block comment")
            out.append(re.sub(r"[^\n]", " ", src[i:j + 2]))
            i = j + 2
            continue
        if ch in "'\"":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
            last_sig = ch
            continue
        if ch == "`":
            stack.append("tpl")
            out.append(ch)
            i += 1
            continue
        if ch == "/" and last_sig in _REGEX_PREV | {"r"}:
            j, in_class = i + 1, False
            while j < n and (in_class or src[j] != "/"):
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            last_sig = "/"
            continue
        if mode == "expr":
            if ch == "{":
                depth[-1] += 1
            elif ch == "}":
                if depth[-1] == 0:
                    stack.pop()
                    depth.pop()
                    out.append(ch)
                    i += 1
                    last_sig = "}"
                    continue
                depth[-1] -= 1
        out.append(ch)
        i += 1
        if not ch.isspace():
            last_sig = ch
    if stack != ["code"]:
        raise ValueError(f"scanner ended inside {stack}")
    return "".join(out)


def strip_css_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), src, flags=re.S)


def ui_text_without_comments(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    if path.suffix in {".js", ".mjs"}:
        return strip_js_comments(src)
    if path.suffix == ".css":
        return strip_css_comments(src)
    src = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group()), src, flags=re.S)
    src = re.sub(r"(<script[^>]*>)(.*?)(</script>)",
                 lambda m: m.group(1) + strip_js_comments(m.group(2)) + m.group(3), src, flags=re.S)
    return re.sub(r"(<style[^>]*>)(.*?)(</style>)",
                  lambda m: m.group(1) + strip_css_comments(m.group(2)) + m.group(3), src, flags=re.S)


class TestLintMachinery(unittest.TestCase):
    """The lint must actually catch things, and must really exempt comments."""

    def test_python_strings_are_caught_but_comments_and_docstrings_are_not(self):
        src = (
            '"""Module doc — allowed."""\n'
            "# comment — allowed\n"
            "def f():\n"
            '    """Doc – allowed."""\n'
            '    a = "bad — dash"\n'
            '    b = f"bad {1} – range"\n'
            '    c = "warn ⚠"\n'
            '    return "Unlock the \\u0041I"  # trailing — allowed\n'
        )
        found = python_string_violations(src)
        self.assertEqual(sorted({ln for ln, _ in found}), [5, 6, 7, 8])

    def test_banned_words(self):
        self.assertTrue(lint_text("an AI judge"))
        self.assertTrue(lint_text("seamless and elevated"))
        self.assertTrue(lint_text("Unlock more"))
        self.assertFalse(lint_text("the aig gateway, said paid"))

    def test_js_scanner_strips_comments_and_keeps_strings(self):
        js = (
            "// line — comment\n"
            "/* block — comment */\n"
            'const a = "keep — me", re = /["\']\\//g, u = "http://x";\n'
            "const t = `x ${y ? `in – ner` : \"\"} tail`; // trailing —\n"
        )
        out = strip_js_comments(js)
        self.assertEqual(out.count("—"), 1)
        self.assertEqual(out.count("–"), 1)
        self.assertIn("http://x", out)

    def test_emoji_ranges(self):
        for ch in ("⚠", "☠", "✓", "\U0001F3BC", "⭐"):
            self.assertTrue(lint_text(f"x {ch}"), repr(ch))
        for ch in ("·", "→", "×", "…", "◆"):
            self.assertFalse(lint_text(f"x {ch}"), repr(ch))


class TestCopyRules(unittest.TestCase):
    def test_python_user_visible_strings(self):
        bad = []
        for path in PY_TARGETS:
            bad += [f"{path.relative_to(ROOT)}:{ln}: {h}"
                    for ln, h in python_string_violations(path.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "\n".join(bad))

    def test_ui_files(self):
        self.assertTrue(UI_TARGETS)
        bad = []
        for path in UI_TARGETS:
            for ln, line in enumerate(ui_text_without_comments(path).splitlines(), 1):
                bad += [f"{path.relative_to(ROOT)}:{ln}: {h}" for h in lint_text(line)]
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
