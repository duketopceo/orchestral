# feat: Make CubeSandbox a durable, tightly-integrated isolated runtime for orchestral

## Summary

The E2B-compatible isolated code runtime works end-to-end against the local
CubeSandbox stack on this host (proven: `code-fizzbuzz` ran its hidden suite
inside a KVM microVM, score 1.0, sandbox reaped). But the working setup is
held together by session-time state: the node IP `192.168.1.123/32` pinned
on `lo` was added by hand, the CA bundle lives in `/tmp`, the `api.cube.app`
route was hand-appended to a config that is *regenerated from its template
on every proxy restart*, wildcard-DNS routing depends on a global resolved
drop-in whose domain claim bleeds onto every sandbox TAP, the control plane
is broadly LAN-exposed, every run needs hand-set env vars, and the three
patches that make VM boot possible sit uncommitted in `~/cubesandbox-src`
where a component upgrade silently overwrites them.

This plan converts the verified working setup into a durable one, then
upstream-submits the host-side patches so durability does not depend on
local binaries surviving upgrades.

## Problem Frame

Bring-up evidence lives in `~/cubesandbox-src/.bringup-evidence/NOTES.md`.
All edges below were observed live on `omarchy-max` (Asahi ARM, kernel 7.1)
or verified against deployed configs, not hypothesized.

- **Boot:** `cube-sandbox-control.target` is enabled and statically
  `Wants=` all services — per-service `disabled` is by design, so the
  stack *does* start on boot. The real cold-boot failure is the node IP:
  `192.168.1.123/32` was pinned on `lo` manually during bring-up. Nothing
  re-adds it, and everything binds or answers it: nginx `listen
  192.168.1.123:8082`, minio publishes `192.168.1.123:9000`, CoreDNS answers
  it for `*.cube.app`, CLM's `CUBE_LCM_CUBEMASTER_URL` uses it. Reboot →
  services come up and fail on a missing address.
- **Config regeneration:** `up-cube-proxy.sh` re-renders `nginx.conf` from
  `nginx.conf.template` via `render_template_atomic` on every proxy start —
  the hand-added `api.cube.app` server block evaporates on any restart, and
  the atomic rewrite's new inode additionally detaches the bind-mounted
  file inside the running container. Same shape for `Corefile` →
  `Corefile.template` on every coredns start. Hand-edits to rendered files
  are never durable; fixes must land in templates plus a container
  recreate, and a package upgrade replaces the toolbox wholesale anyway.
- **DNS:** `/etc/systemd/resolved.conf.d/30-cube-sandbox.conf` sets global
  `DNS=169.254.254.53` + `Domains=~cube.app`. Global `Domains=` applies to
  links with no per-link DNS, so every UP sandbox TAP (named `10.100.x.x`,
  pooled by design — `cleanupTapForReuse`, ~500 exist and stay) claims
  `~cube.app` with no DNS server → intermittent NXDOMAIN, exactly the
  failure observed mid-bring-up. `dns-host-route-up.sh` already sets the
  correct per-link claim on `cube-dns0` — the global drop-in is redundant
  and harmful.
- **TLS:** the mkcert root CA is *already* in the Arch trust store
  (`/etc/ca-certificates/trust-source/anchors/`), which is why curl works.
  Python's httpx uses certifi's static bundle, ignores the OS store, and
  `SSL_CERT_FILE` *replaces* it — so trust needs a durable combined bundle
  at a stable path plus `SSL_CERT_FILE` scoped to cube-using shells, not
  system-trust installation.
- **Auth/exposure:** this install is no-auth (`E2B_API_KEY` arbitrary).
  Verified bind addresses on disk: cube-api `0.0.0.0:3000` (bypasses the
  proxy entirely), cubemaster `0.0.0.0:8089` (`auth.enable: false` — its
  own config comment says "Set to 127.0.0.1 to harden a single all-in-one
  node"), cubelet gRPC `:9999` / HTTP `:9998` / debug `:9966` (insecure
  transport), proxy admin `192.168.1.123:8082` with empty token (accepts
  state/route mutation), CLM `0.0.0.0:8083` empty token, proxy wildcard
  `:80`/`:443`/`:9090` (api block also listens on :80 plaintext), webui
  docker-published `0.0.0.0:12088` (docker chains bypass UFW INPUT),
  minio S3 `192.168.1.123:9000` with static root creds in the compose
  file. UFW's default INPUT policy is DROP which mitigates host-network
  listeners if actually enabled, but docker-published ports traverse the
  DOCKER chain and are LAN-reachable regardless. Any LAN host can create
  sandboxes, exec code, tamper with proxy state, or poison template
  artifacts via minio.
- **Env contract:** `orch()` sets OpenRouter keys only. The working
  configuration (`E2B_DOMAIN=cube.app`, `E2B_API_KEY=...`,
  `ORCHESTRAL_CODE_RUNTIME=isolated`, `SSL_CERT_FILE=<bundle>`,
  `ORCHESTRAL_CUBE_TEMPLATE=code-interpreter`) exists only in session
  history and NOTES.md. Nothing fail-fast validates it before a paid run.
- **Binary fragility:** `containerd-shim-cube-rs` + `cube-runtime` were
  rebuilt with the termios2 seccomp patch and installed into two paths
  (`/usr/local/services/cubetoolbox/...` and
  `/data/cubelet/root/component_versions/cube-shim/v0.7.2/` — template
  builds use the former, sandbox creates the latter). A component upgrade
  writes a *new* `vX.Y.Z` dir and repoints execution — hashing fixed
  v0.7.2 paths can't detect that; the check must resolve the live binary.
  The source patches are **uncommitted** in `~/cubesandbox-src`.
- **SDK drift:** CubeAPI serves only the v1 REST surface (`/v2/sandboxes` →
  405). `orchestral[e2b]` allows `<3`; a self-hosted install that resolves
  e2b 2.x cannot work. PR #109 documents this; nothing enforces it.
- **Egress (live-verified residual):** CubeAPI ignores
  `allow_internet_access=False` — a guest reached pypi.org. Graded worker
  code runs in-guest with egress today; a worker could read the verifier
  oracle from `/home/user` and exfiltrate it, or fetch known solutions.
  `isolated` results are **not** egress-bounded until a CubeEgress policy
  or host rule denies it — treat pass rates on oracle-bearing tasks as
  advisory in the interim.
- **Blocking prerequisite:** PR #109 is open with 2 CodeRabbit comments
  (TypeError-retry on `Sandbox.create`, over-broad teardown wording in
  README) — merge state CLEAN, review `CHANGES_REQUESTED`.

## Requirements

- **R1** — After reboot, services come up on their own and a sandbox can be
  created + exec'd + destroyed with zero manual steps. Requires the `lo`
  node-IP pin to exist before `cube-sandbox-control.target` pulls services.
- **R2** — `getent hosts <anything>.cube.app` resolves deterministically to
  the proxy regardless of sandbox count; `~cube.app` is claimed only by
  `cube-dns0` (+global drop-in if kept), never by TAP links.
- **R3** — The Python e2b SDK trusts `*.cube.app` without `/tmp` state and
  without a globally-exported `SSL_CERT_FILE` — i.e. the var lives only in
  the cube env contract and points at a durable path.
- **R4** — No cube control surface is reachable from off-host: an
  interface-scoped ingress deny covers the full published port set, and
  docker-published binds that bypass UFW are re-scoped at their compose/
  config source. On-host traffic to `192.168.1.123` (lo-pinned) must not
  break — the rule discriminates on ingress interface, not destination
  address. The chosen mechanism is documented in the README self-hosted
  section.
- **R5** — One documented repo-owned artifact provides the full env
  contract, `harness.py doctor` verifies SDK version, endpoint
  reachability, TLS, template presence, and a real create/exec/destroy
  probe (with unconditional kill and short timeout), and a cheap env-shape
  check in the provider-env gate catches misconfiguration before spend.
- **R6** — The three local patches exist as commits in `~/cubesandbox-src`;
  upstream PRs are filed (with the required human DCO step explicitly
  sequenced — upstream CI hard-fails commits lacking `Signed-off-by`,
  which agents must not add).
- **R7** — A patch-integrity check resolves the *active* component-version
  binaries (not fixed v0.7.2 paths), hashes them against a recorded
  manifest, and covers a VM-boot smoke; the re-patch procedure covers both
  binaries and the rendered-config template deltas.
- **R8** — PR #109 review comments resolve and it merges on green CI.
  Gating applies to the *orchestral-side* changes of this plan; host-level
  units (U2–U4) do not wait on the bot review cycle.

## Key Technical Decisions

- **Plan doc lives in orchestral** even though most units touch the host or
  `~/cubesandbox-src`: orchestral is the consumer and the repo where plans
  are tracked. Host changes are procedures, not committed scripts, where
  they touch machine state (dotfiles/chezmoi boundary).
- **Interface-scoped firewall, not destination rules and not CubeAPI auth
  alone:** `api.cube.app` resolves to `192.168.1.123`, which is pinned on
  `lo` — the host's own legitimate traffic targets that address, so a
  destination-scoped deny would break on-host use. Auth on cube-api alone
  doesn't close cubemaster/cubelet/CLM/proxy-admin/webui/minio. Do both:
  ingress deny on the LAN interface for the full port set, re-scope
  docker-published binds at source, and (cheap) set `CUBE_API_KEY` for
  defense-in-depth.
- **Template-first config changes:** anything in `nginx.conf`/`Corefile`
  must be made in `nginx.conf.template`/`Corefile.template` (or upstream
  config inputs) + container recreate, never in the rendered files.
- **`SSL_CERT_FILE` stays, but durable and scoped:** certifi ignores the OS
  trust store; the fix is a combined bundle at a stable path
  (`~/.local/share/cube/` or similar) exported only by `cube-env.sh` —
  not global env, not system-trust installation (a mkcert CA trusted
  system-wide can sign any domain; keep its blast radius scoped anyway).
- **DNS fix is subtraction:** delete `DNS=`/`Domains=` from the global
  drop-in — the per-link `cube-dns0` claim already exists and is the
  designed mechanism.
- **Upstream first, hash-guard second** — patches go upstream as PRs; R7 is
  the safety net while unmerged.
- **No `Signed-off-by` from the agent** — cubesandbox-src AGENTS.md +
  upstream DCO check. U6 sequences the human signoff explicitly.
- **Reboot verification is user-delegated** — cold-boot proof is a
  checklist, not an agent-forced reboot.
- **TAPs are pooled, not leaked** — do not GC them; the only fix needed is
  removing the global `Domains=` claim so they can't poison routing.

## Implementation Units

### U0 — Land PR #109 (orchestral-side prerequisite)

Resolve the two CodeRabbit comments:

- `cubeexec.py` `Sandbox.create` TypeError→ctor retry: replace blind retry
  with signature inspection (same pattern as `_sdk_call`) or bound it so a
  post-allocation TypeError can't double-create. Verify against real
  v1.11 signatures.
- `README.md`: qualify "destroyed on every exit path" → "when the adapter
  holds a sandbox handle; a create-time allocation that raises before
  returning a handle may leave a VM until server-side timeout."

**Verification:** `gh pr checks 109` all pass, review decision clears,
merged. **Files:** `orchestral/cubeexec.py`, `README.md`,
`tests/test_cubeexec.py`.

### U1 — Commit cubesandbox-src patches to a branch

In `~/cubesandbox-src`, `fix/asahi-aarch64-bringup`, three focused commits:
(a) cubelet NUMA sysfs fallback (`Cubelet/pkg/numa/local.go`); (b) ebpf
bump + `AnyTypesByName` port — carries `CubeNet/cubevs/{go.mod,go.sum,
miscs.go}` **and** `Cubelet/go.mod`/`go.sum` (the dep bump spans both
modules); (c) seccomp `TCGETS2`/`TCSETS2` at the **two** patched sites
(`create_vmm_ioctl_seccomp_rule_common`, `create_signal_handler_ioctl_
seccomp_rule` — the pty-foreground rule has no termios entries and was not
patched). `Assisted-by` trailer, no DCO. `.bringup-evidence/` untracked.

**Verification:** `git status` clean; `gofmt -l` empty + `go build` on
touched modules; `cargo fmt --check` + `cargo check` on vmm/shim (upstream
`fmt-check`/`unit-test-check` CI runs these — pre-empt it).

### U2 — Boot durability: the `lo` pin, not unit enablement

- Create a oneshot systemd unit pinning `192.168.1.123/32` on `lo`,
  ordered `Before=cube-sandbox-control.target` (and probably
  `network-online` isn't the ordering lever — the pin is local, order it
  before the control target only).
- Reboot checklist (user-executed): `docker.service` enabled;
  `/data/cubelet` loop mount in fstab (verify — observed present);
  `/dev/kvm`, bpffs available; then the R1 sandbox smoke.
- Do **not** GC the `10.100.x` TAP pool — it's deliberate warmup/reuse
  inventory, not a leak.

**Verification:** `systemctl is-enabled cube-sandbox-control.target`
(enabled) + pin unit installed and ordered correctly (`systemctl
list-dependencies`); user reboot checklist confirms R1.

### U3 — DNS + TLS durability

- DNS: remove `DNS=`/`Domains=` from `/etc/systemd/resolved.conf.d/
  30-cube-sandbox.conf` (or remove the drop-in entirely if it becomes
  empty). Verify TAPs no longer claim `~cube.app`: create + destroy a
  sandbox, `resolvectl status` shows only `cube-dns0` claiming the domain.
  Flush caches; `getent hosts` must resolve `api.cube.app`, arbitrary
  `<sid>.cube.app`, and `<port>-<sid>.cube.app` deterministically.
- TLS: create a durable combined bundle — e.g.
  `~/.local/share/cube/ca-bundle.pem` = system bundle + mkcert root —
  generated by a small script (so it can be refreshed when certifi/system
  CAs update). `cube-env.sh` exports `SSL_CERT_FILE` pointing there. Do
  not export it globally (it would replace certifi for everything).

**Verification:** e2b SDK sandbox create/exec/destroy with `SSL_CERT_FILE`
set to the durable bundle and nothing else; `getent` checks above.

### U4 — Control-plane exposure bound

- Confirm `ufw status` is active; add interface-scoped ingress deny on the
  LAN interface for: 80, 443, 9090 (proxy), 3000 (cube-api), 8089
  (cubemaster), 9998/9999/9966 (cubelet), 8083 (CLM), 12088 (webui —
  docker-published, also needs the compose bind re-scoped to
  `127.0.0.1:12088` at source since docker bypasses UFW INPUT), and
  evaluate minio `192.168.1.123:9000` (re-scope its published bind to
  loopback in `support/docker-compose.yaml` unless LAN access is intended).
- Cheap defense-in-depth: set `CUBE_API_KEY` (cube-api supports simple-key
  unified auth) and `CUBE_LCM_ADMIN_TOKEN`; document that `E2B_API_KEY`
  then must match.
- Never hand-edit `nginx.conf`/`Corefile` — template edits only.
- README self-hosted section: document the exposure model and that no-auth
  is only safe because the control plane is network-bound.

**Verification:** from a real LAN peer (or a netns with a veth to the LAN
bridge — `--interface` self-curl does NOT traverse the ingress chain and
cannot prove the rule), requests to every listed port fail; on-host
`api.cube.app` + envd access still works; `harness.py doctor` (U5) passes.

### U5 — Env contract + doctor preflight (orchestral)

- `harness.py doctor --runtime`: checks e2b SDK present and `<2` when
  `E2B_DOMAIN` is a non-e2b endpoint; resolves `api.$E2B_DOMAIN`; verifies
  TLS handshake; confirms `ORCHESTRAL_CUBE_TEMPLATE` exists via template
  list; runs a create/exec/destroy probe with short timeout and
  unconditional kill. Optional: probe attempts an outbound fetch and
  reports whether egress is open — directly measures the ignored
  `allow_internet_access` flag. Named failure per check, non-zero exit.
- Cheap env-shape check in `_check_provider_envs` (or equivalent gate):
  when `ORCHESTRAL_CODE_RUNTIME=isolated`, require `E2B_DOMAIN`,
  `E2B_API_KEY`, `SSL_CERT_FILE` present before spend.
- `scripts/cube-env.example.sh` committed; `scripts/cube-env.sh`
  gitignored (it will eventually hold a real `E2B_API_KEY`). The example
  carries the full contract incl. the template-alias create command
  (`--with-cube-ca=false` per NOTES.md) and the `.one-click.env` reinstall
  caveat (a `mode=install` wipe resets credentials/template alias — record
  re-setup steps).
- Docs: single canonical env-contract block in README + `docs/task-spec.md`
  cross-ref.

**Verification:** doctor passes live; each injected fault (bad domain,
missing key, missing template, wrong SDK major) fails named; repo gates
(unittest/ruff/mypy) green; new tests for the env-shape check.

### U6 — Upstream submissions (human DCO step required)

Fork + branch from U1's commits. File against TencentCloud/CubeSandbox
(check existing issues first):

- PR: NUMA sysfs fallback (cubelet)
- PR: `cilium/ebpf` v0.22 bump + `AnyTypesByName` port (cubevs + cubelet
  go.mod)
- PR: seccomp termios2 allowlist (vmm)
- Issue: `allow_internet_access=false` ignored (NOTES.md probe evidence)
- Issue/discussion: packaged `nginx.conf.template` lacks an `api.<domain>`
  E2B-gateway route — arguably a missing upstream feature, not a local hack
- Issue/discussion: v1-only REST surface vs e2b SDK v2

The agent prepares branches/commits/PR bodies; **the human must add
`Signed-off-by` (DCO check fails every commit without it), review, and
push/open the PRs.** That handoff is a named step, not implied.

**Verification:** PR/issue URLs recorded in the plan or `docs/solutions/`.

### U7 — Patch-integrity guard

- Script (`scripts/verify-cube-patches.sh` in cubesandbox-src, or a
  toolbox-side file): resolves the *currently-active* shim/runtime binaries
  (follow the component_versions dir that cubelet actually spawns — glob
  `component_versions/cube-shim/*/bin/`, and the cubetoolbox copies),
  sha256-compares against a recorded manifest.
- Manifest also records expected template deltas (`api.cube.app` block in
  `nginx.conf.template`) since `install.sh` replaces the toolbox on upgrade.
- Include a VM-boot smoke: hash equality doesn't prove the shim works if a
  kernel update changes BTF layout again — a template-build or sandbox-
  create probe is the real check.
- Document "run after any cube/omarchy update" in NOTES.md.

**Verification:** swap in an unpatched binary → check fails; restore →
passes; rename to a new version dir → check catches the repoint, not just
the hash.

## High-Level Technical Design

```
U0 (merge #109) ──────────────> orchestral units only
U1 (commit patches) ──┬──> U6 upstream PRs
                      └──> U7 hash guard
U2 (lo pin) ──┐
U3 (dns/tls) ─┼──> U5 doctor + env contract
U4 (exposure)─┘
```

U2–U4 are host-level and parallelizable; U5 needs U2+U3 landed (doctor must
check the final contract) and ideally U4 (so doctor validates the bound
posture). U6/U7 are independent of U2–U5 but need U1.

## System-Wide Impact

- **Security posture change:** U4 removes multiple unauthenticated LAN
  surfaces including sandbox-create RCE, proxy-state mutation, and template
  poisoning via minio. This is the only unit that changes the threat
  boundary.
- **Eval integrity interim caveat:** until a CubeEgress policy exists,
  `isolated` runs have internet egress — pass rates on oracle-bearing tasks
  remain advisory (already documented in README; keep the caveat visible).
- **Cross-repo:** touches `orchestral` (doctor + docs + #109 fixes),
  `~/cubesandbox-src` (3 commits + guard script), host systemd/resolved/
  ufw/compose state, and upstream. Orchestral diff stays small.
- **Verdict channel untouched:** nonce-path runner, stdlib-shadow
  rejection, credential scrubbing from #109 are unchanged.

## Deferred / Out of Scope

- Same-UID detached-process result-file tampering (needs different-UID
  runner in-guest).
- CubeEgress policy authoring for oracle-bearing tasks (verified
  deny-by-default egress is its own unit; interim caveat stands).
- Live CubeSandbox integration lane in CI (needs aarch64+KVM runner).
- Multi-node compute enrollment / Omarchy plugin (`omarchy-plugins`;
  `ONE_CLICK_CONTROL_PLANE_CUBEMASTER_ADDR` mode).
- `[cube]` extra split from `[e2b]` — only if compat complexity grows.
- `orch()` wrapper edits — env contract is repo-owned; personal shell may
  consume `cube-env.sh` but isn't edited here.

## Verification Gates

- Repo gates (orchestral): `scripts/bootstrap-venv.sh /tmp/gate-venv` →
  unittest, ruff, mypy; `audit --strict` + `selfcheck --execute` on spec/
  doc changes.
- End-to-end gate for the whole plan, in a fresh shell with no session env:

```bash
source scripts/cube-env.sh   # repo-owned contract from U5
orch run --task code-fizzbuzz \
  --orchestrator deepseek/deepseek-v4-flash-0731 \
  --worker z-ai/glm-5.3-flash
# expect: execution.runtime=e2b, suite ran in-VM, sandboxes:0 after
```

- Host gates per unit as listed; the cold-boot proof is the U2 user
  checklist.
