"""DUK-290: /api/run/<id> must not ship untruncated prompts or host paths.

The defect: `calls_for_run` was a bare `SELECT *`, so the complete
`{"messages": [...]}` prompt and full completion for every call went out over
HTTP, and `meta.run_dir` carried an absolute host path beside it.

These tests pin the two guarantees and the failure mode a cap invites — a cap
that silently truncates healthy data.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import CALL_PREVIEW_MAX_BYTES, RunStore
from orchestral.web import state

# A canary, not a credential. It only has to be a distinctive string that must
# not appear in a payload; giving it the shape of a provider API key buys the
# test nothing and trips the repo's secret scanner (GitGuardian flagged an
# earlier credential-shaped fixture on this branch, correctly).
_CANARY = "PROMPT-CANARY-4f2a91c7-DO-NOT-LEAK"


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


def _seed_run(runs_dir: str) -> str:
    meta = Runner(dry_run=True, runs_dir=runs_dir, store=RunStore(runs_dir)).run(
        TaskSpec(id="t-task", type="html", prompt="p"),
        _model("o/model", "orchestrator"), _model("w/model", "worker"),
    )
    return meta.run_id


def _overview(store: RunStore) -> dict:
    root = Path(store.root)
    reg = state.JobRegistry(root, root / "tasks", root / "models", store)
    return state.overview_payload(store, reg)


def _record_prompt(store: RunStore, run_id: str, prompt: str, completion: str) -> None:
    store.record_call(
        run_id=run_id, phase="delegate", step=0, role="worker", model="w/model",
        input_json=json.dumps({"messages": [{"role": "user", "content": prompt}]}),
        output_json=json.dumps({"content": completion, "finish_reason": "stop"}),
        finish_reason="stop",
    )


class TestCallPreviewsAreBounded(unittest.TestCase):
    """AC1: the read is bounded, not just the claim about the read."""

    def test_oversized_prompt_is_capped_at_the_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _record_prompt(store, "r1", _CANARY + "x" * 50_000, _CANARY + "y" * 50_000)
            rows = store.call_previews("r1")
            self.assertEqual(len(rows), 1)
            for body, flag, total in (
                ("input_json", "input_truncated", "input_bytes"),
                ("output_json", "output_truncated", "output_bytes"),
            ):
                text = rows[0][body]
                self.assertLessEqual(
                    len(text.encode()), CALL_PREVIEW_MAX_BYTES + 200,
                    f"{body} shipped {len(text.encode())} bytes over the cap")
                self.assertTrue(rows[0][flag], f"{flag} must report the cut")
                self.assertGreater(rows[0][total], CALL_PREVIEW_MAX_BYTES)
                self.assertIn("truncated", text)
                # The marker states the real size, so the cap is auditable.
                self.assertIn(str(rows[0][total]), text)

    def test_cap_bites_inside_sqlite_not_in_python(self):
        """A preview that reads the whole column and slices after the fact
        leaves the disclosure in place for any future caller. The bound has to
        be in the SELECT."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _record_prompt(store, "r1", "a" * 60_000, "b" * 10)
            traced: list[str] = []
            real = store._connect

            @contextmanager
            def spy():
                with real() as conn:
                    conn.set_trace_callback(traced.append)
                    yield conn

            store._connect = spy  # type: ignore[method-assign]
            try:
                rows = store.call_previews("r1")
            finally:
                store._connect = real  # type: ignore[method-assign]

            self.assertTrue(rows[0]["input_truncated"])
            sql = " ".join(traced).upper()
            self.assertIn("SUBSTR", sql)
            self.assertNotIn("SELECT *", sql)
            # An explicit projection: run_id is named, not swept in by *.
            self.assertIn("RUN_ID", sql)

    def test_short_prompt_round_trips_intact(self):
        """Control: the cap must not touch healthy data. A preview that mangles
        every prompt is not a fix, it is a different bug."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            prompt = "Explain the retry budget in one sentence."
            completion = "Three attempts, then escalate."
            _record_prompt(store, "r1", prompt, completion)
            row = store.call_previews("r1")[0]
            self.assertFalse(row["input_truncated"])
            self.assertFalse(row["output_truncated"])
            self.assertNotIn("truncated", row["input_json"])
            self.assertEqual(
                json.loads(row["input_json"])["messages"][0]["content"], prompt)
            self.assertEqual(json.loads(row["output_json"])["content"], completion)
            self.assertEqual(row["input_bytes"], len(
                json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode()))

    def test_envelope_columns_survive_the_projection(self):
        """Cost, tokens and model are what the run view is actually for; the cap
        must not cost us the ledger."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.record_call(
                run_id="r1", phase="plan", step=1, role="orchestrator",
                model="o/model", input_tokens=120, output_tokens=34,
                cost_usd=0.0123, latency_ms=420.0, worker_id="worker-0",
                sequence=7, input_json='{"messages": []}', output_json="{}",
                finish_reason="stop",
            )
            row = store.call_previews("r1")[0]
            for key, want in (
                ("call_id", 1), ("run_id", "r1"), ("phase", "plan"),
                ("model", "o/model"), ("input_tokens", 120), ("output_tokens", 34),
                ("worker_id", "worker-0"), ("sequence", 7),
                ("finish_reason", "stop"),
            ):
                self.assertEqual(row[key], want, f"{key} missing from the projection")
            self.assertAlmostEqual(row["cost_usd"], 0.0123)
            self.assertAlmostEqual(row["latency_ms"], 420.0)

    def test_multibyte_prompt_is_measured_in_bytes(self):
        """A cap in characters lets a CJK prompt through at 3x the byte budget,
        which is the budget the docstring claims. The row is written with
        real multi-byte UTF-8 (not json.dumps' \\uXXXX escapes, which would
        make the column pure ASCII and prove nothing)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            body = json.dumps(
                {"messages": [{"role": "user", "content": "密" * 20_000}]},
                ensure_ascii=False)
            store.record_call(
                run_id="r1", phase="delegate", step=0, role="worker",
                model="w/model", input_json=body, output_json="{}",
            )
            row = store.call_previews("r1")[0]
            self.assertEqual(row["input_bytes"], len(body.encode()))
            self.assertTrue(row["input_truncated"])
            self.assertLessEqual(
                len(row["input_json"].encode()), CALL_PREVIEW_MAX_BYTES + 200)
            # The character count is well inside the cap; only a byte cap bites.
            self.assertLess(len(row["input_json"]), CALL_PREVIEW_MAX_BYTES)

    def test_error_is_already_bounded_at_the_write(self):
        """The projection passes `error` through whole, so this pins where that
        is safe: both write paths store `(error or "")[:500]`, under the cap.
        Lift the write-side bound and this test is the reminder that the
        projection then needs one too."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.record_call(
                run_id="r1", phase="delegate", step=0, role="worker",
                model="w/model", error="E" * 40_000, error_category="rate_limit",
            )
            row = store.call_previews("r1")[0]
            self.assertEqual(row["error"], "E" * 500)
            self.assertEqual(row["error_category"], "rate_limit")
            self.assertLessEqual(len(row["error"].encode()), 500)

    def test_full_bodies_still_reachable_for_export(self):
        """dataset.py needs the untruncated ledger; the preview must not be the
        only door, and must not become one by accident."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _record_prompt(store, "r1", "z" * 30_000, "y" * 30_000)
            full = store.calls_for_run("r1")[0]
            self.assertIn("z" * 30_000, full["input_json"])
            self.assertIn("y" * 30_000, full["output_json"])


class TestRunPayloadShipsNoHostPath(unittest.TestCase):
    """AC2: no absolute host path in any HTTP payload."""

    def test_run_detail_meta_has_no_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            payload = state.run_detail_payload(store, rid)
            self.assertNotIn("run_dir", payload["meta"])
            blob = json.dumps(payload)
            self.assertNotIn(str(Path(tmp).resolve()), blob)
            self.assertNotIn(str(tmp), blob)

    def test_runs_and_overview_payloads_have_no_run_dir(self):
        """The leak is not confined to one route: /api/runs and /api/overview
        both serialised RunMeta. Fixing only /api/run leaves it reachable."""
        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            store = RunStore(tmp)
            for name, payload in (
                ("runs_payload", state.runs_payload(store)),
                ("overview_payload", _overview(store)),
            ):
                blob = json.dumps(payload, default=str)
                self.assertNotIn(
                    str(Path(tmp).resolve()), blob, f"{name} leaked the store root")
                self.assertTrue(_meta_rows(payload), f"{name} had no RunMeta rows")
                for row in _meta_rows(payload):
                    self.assertNotIn("run_dir", row, f"{name} leaked run_dir")

    def test_on_disk_meta_keeps_run_dir(self):
        """_write_meta_file needs the real path; stripping it everywhere would
        break resume and update_meta."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            meta = store.get_run(rid)
            self.assertTrue(meta.run_dir)
            self.assertTrue(Path(meta.run_dir, "run.json").exists())
            self.assertIn("run_dir", json.loads(
                Path(meta.run_dir, "run.json").read_text()))
            self.assertIn("run_dir", meta.to_dict())
            self.assertNotIn("run_dir", meta.to_public_dict())

    def test_to_public_dict_is_to_dict_minus_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            meta = RunStore(tmp).get_run(rid)
            full = meta.to_dict()
            public = meta.to_public_dict()
            self.assertEqual(set(public), set(full) - {"run_dir"})
            self.assertEqual({k: public[k] for k in public},
                             {k: full[k] for k in public})


def _meta_rows(payload) -> list[dict]:
    """Every nested RunMeta-shaped dict in a payload, at any depth."""
    found: list[dict] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "run_id" in node and "orchestrator" in node:
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


class TestRunDetailCallsArePreviews(unittest.TestCase):
    """AC1 at the route boundary, not just the helper."""

    def test_route_payload_drops_body_content_past_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            filler = "q" * 30_000
            _record_prompt(store, rid, f'{{"pad":"{filler}","key":"{_CANARY}"}}',
                           f'{{"pad":"{filler}","key":"{_CANARY}"}}')
            payload = state.run_detail_payload(store, rid)
            self.assertNotIn(_CANARY, json.dumps(payload))
            call = next(c for c in payload["calls"] if c["input_truncated"])
            self.assertTrue(call["output_truncated"])

    def test_cap_is_a_head_prefix_not_a_redaction(self):
        """Stated, not hidden: the cap keeps the first `max_bytes` of a body.
        That is a preview, so a secret at the head of a prompt still ships —
        the guarantee is bounded size, not redaction. If this ever needs to be
        redaction, the fix is omitting the body, not a bigger cap."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            _record_prompt(store, rid, _CANARY + "q" * 30_000, _CANARY)
            payload = state.run_detail_payload(store, rid)
            self.assertIn(_CANARY, json.dumps(payload))
            call = next(c for c in payload["calls"] if c["input_truncated"])
            self.assertLessEqual(
                len(call["input_json"].encode()), CALL_PREVIEW_MAX_BYTES + 200)

    def test_route_still_reports_cost_and_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            before = len(state.run_detail_payload(store, rid)["calls"])
            store.record_call(
                run_id=rid, phase="judge", step=0, role="orchestrator",
                model="o/model", input_tokens=900, output_tokens=210,
                cost_usd=0.25, input_json='{"messages": [{"role": "user", "content": "hi"}]}',
                output_json="{}", finish_reason="stop",
            )
            calls = state.run_detail_payload(store, rid)["calls"]
            # The seeded dry-run already wrote calls; take ours, not calls[0].
            call = next(c for c in calls if c["phase"] == "judge")
            self.assertEqual(call["input_tokens"], 900)
            self.assertEqual(call["output_tokens"], 210)
            self.assertAlmostEqual(call["cost_usd"], 0.25)
            # ...and the pre-existing ledger is still whole, not dropped.
            self.assertEqual(len(calls), before + 1)


class TestDocstringsMatchTheCode(unittest.TestCase):
    """AC3: the promises a reviewer signs off on have to be checkable."""

    def test_transcript_docstring_cites_the_enforcement_point(self):
        doc = state._event_transcript.__doc__ or ""
        self.assertIn("prompt", doc.lower())
        self.assertIn("call_previews", doc,
                      "the docstring must name where bodies are actually bounded")

    def test_artifact_docstring_states_it_reads_no_member_bytes(self):
        doc = (state.artifact_info.__doc__ or "").lower()
        self.assertIn("never as bodies", doc)
        self.assertIn("infolist", doc)

    def test_artifact_info_really_reads_no_member_bytes(self):
        """Holds the docstring to its word: a zip whose first member is huge
        must come back as a listing, not a body."""
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with zipfile.ZipFile(run_dir / "artifact.zip", "w") as zf:
                zf.writestr("big.txt", "P" * 200_000)
                zf.writestr("small.txt", "s")
            info = state.artifact_info(run_dir)
            self.assertEqual(
                [m["name"] for m in info["members"]], ["big.txt", "small.txt"])
            self.assertLess(len(json.dumps(info)), 1000)
            self.assertNotIn("PPPP", json.dumps(info))


if __name__ == "__main__":
    unittest.main()
