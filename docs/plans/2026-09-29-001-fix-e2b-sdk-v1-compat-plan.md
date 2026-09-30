---
title: "fix: e2b SDK v1/v2 compatibility for the isolated code runtime"
type: fix
date: 2026-09-29
---

# fix: e2b SDK v1/v2 compatibility for the isolated code runtime

## Summary

The merged adapter pins `e2b>=2.1,<3`, but CubeSandbox's CubeAPI serves only
the E2B v1 REST surface (`POST /sandboxes`; SDK v2 calls `/v2/sandboxes` and
gets 405), so `ORCHESTRAL_CODE_RUNTIME=isolated` cannot work against it as
shipped. This plan lands the already-verified compat layer in
`orchestral/cubeexec.py`, regression-tests each compat branch with a fake SDK,
corrects the pin to `e2b>=1.11,<3`, and documents the self-hosted deployment
contract — including two findings from live probing: CubeAPI ignores
`allow_internet_access=False`, and self-hosted installs must constrain the
SDK to v1 (`e2b<2`) because the shims bridge the Python API surface, not the
wire protocol.

## Problem Frame

CubeSandbox bring-up on an aarch64 host exposed SDK-surface incompatibilities,
all verified live against a working deployment (sandbox
`ee60f8fdbdac46c7810ea5853b36cd76` on a local cube.app endpoint):

- v1 exports `CommandExitException` at the `e2b` top level, not
  `e2b.exceptions`.
- v1 has no `Sandbox.create()` classmethod; the constructor is the entry point.
- v1 `files.write` has no `request_timeout` kwarg — the generated signature
  takes it positionally as `(path, data, user, request_timeout)`.
- v1 serializes the command timeout header as `timeout*1000`, so a float
  arrives as `"30000.0"` and envd's `strconv.ParseInt` rejects it.
- `e2b.exceptions` may not exist on every v1-compatible distribution — the
  unguarded `TimeoutException` import would misreport "SDK not installed".

Two properties verified live during review:

- **Egress is open on CubeSandbox despite `allow_internet_access=False`.** A
  sandbox ran `curl https://pypi.org` successfully (HTTP 200). The v1 SDK
  sends the flag in the create body; CubeAPI ignores it. Egress control on
  self-hosted installs is deployment-level (CubeEgress / host firewall), so
  the README's "no egress" claim is false for CubeSandbox until verified
  there.
- **`kill()` return is not a reliable outcome signal on v1.** A falsy return
  was observed even though the sandbox was actually destroyed (confirmed via
  `GET /sandboxes` → empty). The `not_found` label records what the SDK
  reported, not ground truth.

The fixes exist in the working tree and are proven end-to-end: a real
`code-fizzbuzz` run executed its hidden suite inside a microVM
(`runtime: "e2b"`, 5 tests, `ok: true`, score 1.0, sandbox destroyed). What is
missing is unit coverage for the compat branches, the dependency pin
correction, a `kill()` timeout-kwarg shim for consistency, and honest docs for
self-hosted endpoints.

## Requirements

- R1. The adapter works with e2b SDK v1.x (CubeAPI/self-hosted) and v2.x
  (hosted E2B) without runtime detection or configuration.
- R2. The `e2b` extra permits both majors (`>=1.11,<3`); docs make clear that
  self-hosted CubeSandbox requires the v1 line (`e2b<2`).
- R3. Each compat branch has a unit test using the fake-SDK harness — no live
  sandbox required in CI.
- R4. The self-hosted deployment contract is documented: `E2B_DOMAIN`
  semantics for CubeSandbox, template alias, auth posture, TLS/CA, DNS
  prerequisites, the v1 SDK constraint, and the egress caveat.
- R5. The report contract is unchanged — same keys, same fail-closed
  semantics, `runtime: "e2b"` provenance.
- R6. Docs stop claiming "no egress" as an adapter guarantee on self-hosted
  installs; they state where egress is actually enforced.

## Key Technical Decisions

- **Duck-typed compat shims over a version gate**: dispatch on the actual
  surface (`getattr(Sandbox, "create", None)`, `TypeError` fallback on the
  kwarg), never on `e2b.__version__`. The signature is the contract; version
  strings lie across dist builds and forks.
- **Both majors supported over v1-only**: the adapter contract is "any
  E2B-compatible endpoint". Rejected pinning to v1-only (breaks installs that
  already carry v2 for hosted E2B) and keeping v2-only (the bug this fixes).
- **Shim the SDK rather than call the REST surface directly**: rejected a
  small `httpx` client against the five-call v1 surface — the SDK owns the
  `E2B_DOMAIN` host-derivation contract (`api.<domain>` vs
  `<port>-<id>.<domain>`), auth handling, and stdout streaming; a hand-rolled
  client would have to re-derive all three and would lose hosted-E2B support.
- **Integer, floored command timeout**: v1 multiplies the timeout into an
  integer-parse header; `max(1, int(min(timeout_seconds, remaining())))`
  casts and applies the same `>=1` floor `request_timeout()` already uses —
  a sub-second `remaining()` would otherwise send `timeout=0`, whose envd
  semantics are undefined.
- **`sandbox_cleanup: "not_found"` label stays**: v1 `kill()` can return falsy
  even when teardown succeeded — verified externally that no sandbox leaks.
  The label records the SDK-reported outcome, and code comments say exactly
  that. Rejected a post-kill list-verification call: extra request, new
  failure mode, cosmetic gain.
- **`kill()` gets the same positional fallback as `files.write`**: a v1 build
  that rejects `request_timeout=` on `kill` would otherwise report
  `kill_failed` and leak the microVM (with the oracle source inside) until
  TTL. Same generated-signature problem, same shim shape.

## Implementation Units

### U1. Land the compat layer in `orchestral/cubeexec.py`

**Goal:** Make the adapter dispatch correctly across SDK v1 and v2.

**Requirements:** R1, R2, R5

**Dependencies:** none

**Files:**

- `orchestral/cubeexec.py` (modify — the working-tree diff plus two small
  hardenings below)
- `pyproject.toml` (modify — `e2b` extra pin to `e2b>=1.11,<3` with a comment
  naming the CubeAPI v1-only wire constraint)

**Approach:** Already implemented and verified live; this unit commits it plus
two hardenings surfaced by review. Seams: exception-import fallback in
`_load_sdk` (v2 `e2b.exceptions` → v1 top-level `CommandExitException`);
`TimeoutException` import gets a fallback too — if `e2b.exceptions` is absent
or lacks it, fall back to `e2b.TimeoutException` then builtin `TimeoutError`,
and report "installed but incompatible surface" rather than "not installed"
when only the sandbox class is missing; `create` classmethod vs constructor
dispatch; `_files_write` helper (kwarg → positional
`(path, body, "user", timeout)`); apply the identical fallback to `kill()`;
`max(1, int(min(timeout_seconds, remaining())))` on the `commands.run`
timeout arg. Add a comment on `_kill_sandbox` stating v1's return is not a
reliable outcome signal.

**Patterns to follow:** existing module structure in
`orchestral/cubeexec.py`; optional-extra dependency comments in
`pyproject.toml`.

**Test scenarios:** covered by U2 — this unit carries no standalone tests.

**Verification:** the module imports cleanly and the fake-SDK suite passes.
The pin admits both majors: `pip install '.[e2b]'` resolves to a v2.x build
(upper bound intact) and `pip install '.[e2b]' 'e2b<2'` resolves a v1.x —
both configurations must leave `unittest discover` green via the fake-SDK
suite.

### U2. Regression tests for the compat branches

**Goal:** Each SDK-surface branch in U1 is exercised by a unit test.

**Requirements:** R3, R5

**Dependencies:** U1

**Files:**

- `tests/test_cubeexec.py` (modify)

**Approach:** Extend the existing fake-`e2b` injection with a v1-shaped
variant. The current `_install_fake_e2b` models v2 (exceptions under
`e2b.exceptions`, `Sandbox.create` classmethod, kwargs-accepting
`files.write`); add a parallel v1 fixture.

**Test scenarios:**

- v1 import path: fake `e2b` exports `CommandExitException` at top level and
  `e2b.exceptions` lacks it; `_load_sdk()` returns a usable tuple and a full
  `run_unittest_suite` passes.
- Missing `e2b.exceptions` entirely: report error distinguishes
  "installed but incompatible surface" from "not installed".
- v1 sandbox construction: fake `Sandbox` without a `create` classmethod;
  assert the constructor was used and the suite ran.
- v1 `files.write` fallback: fake `files.write` that raises `TypeError` on a
  `request_timeout` kwarg and records positional args; assert the fallback
  form `(path, body, "user", timeout)` was used and the suite still passes.
- v1 `kill` fallback: fake `kill` that raises `TypeError` on the kwarg;
  assert the positional retry ran and cleanup is reported honestly.
- Integer, floored timeout: assert `commands.run` received `timeout` as `int`
  and `>= 1`, including a near-deadline path where `remaining()` is in (0, 1).
- v2 path still works: existing tests already cover it — assert they still
  pass unchanged.

**Verification:** new tests fail if any compat seam is reverted; full
`unittest discover` green.

### U3. Document the self-hosted endpoint contract and correct stale claims

**Goal:** An operator can point the adapter at a self-hosted CubeSandbox from
the docs alone, and the docs stop overstating what the adapter guarantees.

**Requirements:** R4, R6

**Dependencies:** U1

**Files:**

- `README.md` (modify — extend the isolated-runtime paragraph)
- `docs/task-spec.md` (modify — correct the runner description, pointer to
  the README section)

**Approach:** Add a short "self-hosted CubeSandbox" subsection covering:

- Install: `pip install "orchestral[e2b]" "e2b<2"` — CubeAPI serves the v1
  wire surface only; a default resolve picks v2.x and gets 405 on
  `/v2/sandboxes`.
- `E2B_DOMAIN=cube.app` semantics: the SDK builds `api.<domain>` for the
  control plane and `<port>-<sandbox-id>.<domain>` for envd — one fixed host
  plus one wildcard shape; wildcard DNS and a trusted CA are prerequisites.
- `ORCHESTRAL_CUBE_TEMPLATE` names the template alias created via
  `cubemastercli`.
- `E2B_API_KEY` is required-but-arbitrary on no-auth installs — and a no-auth
  endpoint grants unauthenticated sandbox create/write/exec to any host that
  can resolve `api.<domain>`; it must be bound to trusted networks only.
- `SSL_CERT_FILE` must *append* a local CA to the system bundle rather than
  replace it, and it applies process-wide (to OpenRouter calls too) — prefer
  a narrowly-scoped CA.
- Egress: `allow_internet_access=False` is an adapter request honored by
  hosted E2B; CubeAPI ignores it (verified live — guest reached pypi.org).
  On self-hosted installs, egress denial must come from CubeEgress or the
  host firewall, and the hidden-test oracle sits inside the guest — treat
  oracle-bearing tasks as needing egress control proven at deployment level.
- In `docs/task-spec.md`, replace "`python3 -Es -m unittest` runs there" with
  a description of the verifier-authored runner (derives the verdict from the
  unittest result object, writes a nonce-named payload file) — that is the
  load-bearing anti-forgery property.

**Patterns to follow:** the existing isolated-runtime prose block in
`README.md`.

**Test scenarios:**

- Docs name every env var, both host shapes, the `e2b<2` constraint, the
  no-auth exposure caveat, and the egress caveat — checked by review, not
  code.

**Verification:** a reader following only README can name every env var,
infra prerequisite, and security caveat without other sources.

## Scope Boundaries

Out of scope: CubeSandbox host bring-up itself (kernel/KVM/DNS/nginx work
lives outside this repo), upstream PRs to `TencentCloud/CubeSandbox`
(including the ignored `allow_internet_access` flag — worth reporting
upstream), and any `runs/` artifacts.

### Deferred to Follow-Up Work

- A live-integration CI lane or smoke script against a CubeSandbox endpoint —
  needs a deployed node; fake-SDK coverage is the regression contract until
  one exists.
- A deployment-level egress probe as part of that lane (guest-side connect
  that must fail once CubeEgress/firewall is configured).
- Multi-node/Omarchy enrollment documentation — belongs to the plugin
  project, not this adapter.
- Possibly splitting the `e2b` extra (`[e2b]` for hosted v2, `[cube]` for
  self-hosted v1) if the dual-major shim surface grows.

## Post-Implementation Review Disposition

Code review (correctness/security/reliability/standards personas) after U1–U3
surfaced and this branch now includes, beyond the planned scope:

- **P1 fixed — stdlib-shadowing forgery channel.** The verifier runner and
  result payload now live at nonce-named `/tmp` paths, outside the worker
  fileset dir, so a member like `json.py` can no longer shadow stdlib for the
  runner's imports. Top-level members matching `sys.stdlib_module_names` are
  rejected before sandbox write.
- `_sdk_call` dispatches on `inspect.signature` instead of blanket `TypeError`
  retry — closes the double-write/double-kill window.
- `_load_sdk` runs inside the outer try (non-`ImportError` load failures now
  produce a report, not a raise); `exc_class` verifies class-hood;
  `create()`→ctor `TypeError` fallback.
- Kill attempts bounded by `remaining()`; deadline check inside the write
  loop; `except timeout_exc` no longer mislabels transport `TimeoutError`;
  payload int-coercion inside the guarded try; `result.exit_code` captured;
  `E2B_DOMAIN` scrubbed from persisted errors; case-folding members
  (`Main.java`) rejected honestly.

Deferred residual findings (recorded, not blocking):

- Parallel `files.write` concurrency (efficiency) — v1 SDK thread-safety
  unverified; revisit with the live-integration lane.
- Same-uid result-file tampering window — documented in the module docstring;
  closing it needs a different-uid suite runner (envd `user` plumbing).
- Upstream report: CubeAPI ignores `allow_internet_access` (live-probed).
