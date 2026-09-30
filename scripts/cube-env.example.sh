# Orchestral <-> self-hosted CubeSandbox environment contract.
#
# Usage: copy to scripts/cube-env.sh (gitignored), fill in real values, then
#   source scripts/cube-env.sh
# before `harness.py run ...` / `orch run ...` invocations that should grade
# code tasks inside microVMs instead of failing closed.
#
# `harness.py doctor` verifies this whole contract before spend.

# Route isolated code execution to the E2B-compatible adapter.
export ORCHESTRAL_CODE_RUNTIME=isolated

# The SDK builds api.<domain> (control plane) and <port>-<sandbox-id>.<domain>
# (envd) from this. Wildcard DNS + a locally trusted CA are prerequisites.
export E2B_DOMAIN=cube.app

# CubeAPI honors CUBE_API_KEY when the deployment sets it — E2B_API_KEY must
# match. On no-auth installs any non-empty string works, but no-auth is only
# safe when the control plane is network-bound (firewalled to trusted
# interfaces). Source this from a secret manager, not a committed file —
# e.g. omaseal, 1Password, direnv.
export E2B_API_KEY="CHANGEME"

# certifi ignores the OS trust store, and SSL_CERT_FILE *replaces* the bundle —
# so point it at a durable combined bundle (system CAs + the deployment's
# local CA), never a bundle missing public roots. Export it only in shells
# that run evals; a global export would affect every Python HTTPS call.
#
# Build once (Arch example — adjust bundle path per distro):
#   cat /etc/ssl/certs/ca-certificates.crt /path/to/local-rootCA.pem \
#       > ~/.local/share/cube/ca-bundle.pem
export SSL_CERT_FILE="$HOME/.local/share/cube/ca-bundle.pem"

# Sandbox template alias on the node (optional; adapter default is
# code-interpreter). Create it with the deployment's tooling, e.g.:
#   cubemastercli tpl create-from-image --with-cube-ca=false ...
export ORCHESTRAL_CUBE_TEMPLATE=code-interpreter
