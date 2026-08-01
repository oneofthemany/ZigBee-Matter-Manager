#!/bin/bash
# =============================================================================
# ZMM — Regenerate requirements.lock from requirements.txt
# =============================================================================
set -euo pipefail

# ── Colours (match build.sh) ────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}${BOLD}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}${BOLD}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}${BOLD}[ERR ]${NC}  $*" >&2; exit 1; }

# ── Locate repo root (this script lives in scripts/) ───────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ_TXT="${REPO_ROOT}/requirements.txt"
REQ_LOCK="${REPO_ROOT}/requirements.lock"

[[ -f "$REQ_TXT" ]] || die "requirements.txt not found at ${REQ_TXT}"

# ── Ensure uv is available ──────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    die "uv not found. Install it with:
        pip install uv
      or:
        curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# ── Compile ──────────────────────────────────────────────────────────────────
info "Compiling requirements.txt -> requirements.lock ..."
(
    cd "$REPO_ROOT"
    uv pip compile requirements.txt \
        --python-version 3.11 \
        --python-platform x86_64-manylinux_2_31 \
        -o requirements.lock
)

# ── Drift check (same logic as upgrade.sh do_build) ─────────────────────────
drift=""
while IFS= read -r pkg; do
    [[ -z "$pkg" ]] && continue
    pkg_re=$(printf '%s' "$pkg" | sed 's/[-_]/[-_]/g')
    grep -qiE "^${pkg_re}==" "$REQ_LOCK" || drift="${drift} ${pkg}"
done < <(grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$REQ_TXT" \
         | sed -E 's/\[[^]]*\]//; s/[<>=!~;].*//; s/[[:space:]\r]+//g' | sort -u)

if [[ -n "$drift" ]]; then
    die "Lock regenerated but still missing pins for:${drift}
      This usually means uv could not resolve them for py3.11/manylinux_2_31."
fi
ok "Lock is in sync — every requirements.txt package is pinned."

# ── Summary of what changed ──────────────────────────────────────────────────
if command -v git &>/dev/null && git -C "$REPO_ROOT" rev-parse --git-dir &>/dev/null; then
    if git -C "$REPO_ROOT" diff --quiet -- requirements.lock; then
        ok "requirements.lock unchanged (already up to date)."
    else
        info "requirements.lock changes:"
        git -C "$REPO_ROOT" diff --stat -- requirements.lock | sed 's/^/    /'
        git -C "$REPO_ROOT" diff -- requirements.lock \
            | grep -E '^[+-][a-zA-Z0-9]' | head -40 | sed 's/^/    /'
        echo
        warn "Commit requirements.txt and requirements.lock together."
    fi
fi
