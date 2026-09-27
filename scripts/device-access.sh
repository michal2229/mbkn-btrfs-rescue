#!/usr/bin/env bash
# Temporarily grant the current user READ-ONLY access to a block device, and freeze it.
#
#   scripts/device-access.sh grant  [DEVICE]   # blockdev --setro + ACL u:$USER:r
#   scripts/device-access.sh revoke [DEVICE]   # remove the ACL (device stays read-only)
#   scripts/device-access.sh status [DEVICE]
#   scripts/device-access.sh unfreeze [DEVICE] # blockdev --setrw (only when recovery is done)
#
# DEVICE defaults to `device` from the config. Both changes are runtime-only and disappear
# on reboot or when the device-mapper/LUKS mapping is closed. Uses sudo.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"
DEVICE="${2:-}"
USER_NAME="$(id -un)"

if [[ -z "$DEVICE" ]]; then
    DEVICE="$(cd "$REPO" && uv run --no-sync python -c \
        'from mbkn_btrfs_rescue.config import load_config; print(load_config().device or "")' \
        2>/dev/null || true)"
fi
[[ -n "$DEVICE" ]] || { echo "no device given and none in config" >&2; exit 64; }
REAL="$(readlink -f "$DEVICE")"
[[ -b "$REAL" ]] || { echo "$DEVICE is not a block device" >&2; exit 66; }

status() {
    echo "device     $DEVICE -> $REAL"
    echo "read-only  $(sed 's/1/yes (frozen)/;s/0/NO/' "/sys/class/block/$(basename "$REAL")/ro")"
    echo "mounted    $(findmnt -rn -S "$REAL" -o TARGET | tr '\n' ' ' || true)"
    getfacl -p "$REAL" 2>/dev/null | grep -E '^user:' || true
    if [[ -r "$REAL" ]]; then echo "readable   yes (by $USER_NAME)"; else echo "readable   no"; fi
}

case "$ACTION" in
    grant)
        if findmnt -rn -S "$REAL" >/dev/null; then
            echo "WARNING: $REAL is mounted. Unmount it first to stop further writes." >&2
        fi
        sudo blockdev --setro "$REAL"
        sudo setfacl -m "u:${USER_NAME}:r" "$REAL"
        status
        ;;
    revoke)
        sudo setfacl -x "u:${USER_NAME}" "$REAL"
        status
        ;;
    unfreeze)
        sudo blockdev --setrw "$REAL"
        status
        ;;
    status)
        status
        ;;
    *)
        sed -n '2,10p' "$0"
        exit 64
        ;;
esac
