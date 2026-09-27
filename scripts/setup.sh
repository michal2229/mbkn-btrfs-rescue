#!/usr/bin/env bash
# Set up mbkn-btrfs-rescue: uv environment, local config, tmp/cache dirs.
#
#   scripts/setup.sh [--device /dev/mapper/...] [--no-fuse] [--config PATH]
#
# Safe to re-run. Never touches the device.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVICE=""
FUSE=1
CONFIG="$REPO/mbkn-btrfs-rescue.toml"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device) DEVICE="$2"; shift 2 ;;
        --no-fuse) FUSE=0; shift ;;
        --config) CONFIG="$2"; shift 2 ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 64 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found. Install it with one of:"
    warn "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    warn "  brew install uv   |   pipx install uv   |   your distro package"
    exit 1
fi
info "uv $(uv --version | cut -d' ' -f2)"

# 2. system tools (informational)
for tool in mkfs.btrfs fusermount3 setfacl blockdev; do
    command -v "$tool" >/dev/null 2>&1 || warn "optional tool not found: $tool"
done

# 3. environment
cd "$REPO"
if [[ $FUSE -eq 1 ]]; then
    info "uv sync --extra fuse"
    uv sync --extra fuse
else
    info "uv sync"
    uv sync
fi

# 4. config
if [[ ! -f "$CONFIG" ]]; then
    info "creating $CONFIG from example"
    mkdir -p "$(dirname "$CONFIG")"
    cp "$REPO/mbkn-btrfs-rescue.example.toml" "$CONFIG"
fi
if [[ -n "$DEVICE" ]]; then
    info "setting device = $DEVICE"
    tmp="$(mktemp "${CONFIG}.XXXX")"
    grep -v -E '^[#[:space:]]*device[[:space:]]*=' "$CONFIG" > "$tmp" || true
    { printf 'device = "%s"\n' "$DEVICE"; cat "$tmp"; } > "$CONFIG"
    rm -f "$tmp"
fi

# 5. directories from config
uv run --no-sync python - "$CONFIG" <<'PY'
import sys
from mbkn_btrfs_rescue.config import load_config
cfg = load_config(sys.argv[1])
cfg.prepare_dirs()
print(f"   config    {cfg.source}\n   device    {cfg.device or '(not set)'}")
print(f"   tmp_dir   {cfg.tmp_dir}\n   cache_dir {cfg.cache_dir}\n   index     {cfg.db_path}")
PY

# 6. git hooks: static checks on commit, tests on push
if git -C "$(dirname "$0")/.." rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$(dirname "$0")/.." config core.hooksPath scripts/git-hooks
    info "git hooks enabled (scripts/git-hooks: checks on commit, tests on push)"
fi

info "done. Next: scripts/device-access.sh grant <device>, then: uv run mbkn-btrfs-rescue info"
