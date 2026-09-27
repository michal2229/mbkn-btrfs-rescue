#!/usr/bin/env bash
# Build btrfs test images in <tmp_dir>/images.
#
#   scripts/make-test-image.sh simple    # no root: mkfs.btrfs --rootdir with a _work subvolume
#   scripts/make-test-image.sh realistic # sudo: loop-mount, write in several transactions,
#                                        # then `rm -rf` a project and delete a subvolume
#
# Options: --size 1G  --compress zstd|lzo|zlib|no  --discard (realistic: mount with discard,
# to see what TRIM does to recoverability)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-simple}"; shift || true
SIZE=1G
COMPRESS=zstd
DISCARD=nodiscard
while [[ $# -gt 0 ]]; do
    case "$1" in
        --size) SIZE="$2"; shift 2 ;;
        --compress) COMPRESS="$2"; shift 2 ;;
        --discard) DISCARD=discard; shift ;;
        *) echo "unknown option: $1" >&2; exit 64 ;;
    esac
done

TMP_DIR="$(cd "$REPO" && uv run --no-sync python -c \
    'from mbkn_btrfs_rescue.config import load_config; print(load_config().tmp_dir)')"
OUT="$TMP_DIR/images/$MODE"
rm -rf "$OUT"
mkdir -p "$OUT/src"
IMG="$OUT/fs.img"

populate() {  # $1 = target dir; creates a small fake project tree
    local d="$1"
    mkdir -p "$d/proj/src/pkg" "$d/proj/docs" "$d/.venv/lib/python3/site-packages/x"
    for i in $(seq 1 40); do
        printf '# module %s\ndef f%s():\n    return %s\n' "$i" "$i" "$i" > "$d/proj/src/pkg/mod_$i.py"
    done
    seq 1 200000 | sed 's/^/log line /' > "$d/proj/docs/big.log"
    head -c 2000000 /dev/urandom > "$d/proj/docs/blob.bin"
    echo "venv junk" > "$d/.venv/lib/python3/site-packages/x/__init__.py"
    ln -sf src/pkg/mod_1.py "$d/proj/link.py"
}

case "$MODE" in
    simple)
        populate "$OUT/src/_work"
        truncate -s "$SIZE" "$IMG"
        mkfs.btrfs -q -K -r "$OUT/src" -u rw:_work --compress "$COMPRESS" "$IMG"
        ;;
    realistic)
        truncate -s "$SIZE" "$IMG"
        mkfs.btrfs -q -K "$IMG"
        MNT="$OUT/mnt"; mkdir -p "$MNT"
        LOOP="$(sudo losetup --find --show "$IMG")"
        trap 'sudo umount "$MNT" 2>/dev/null || true; sudo losetup -d "$LOOP" 2>/dev/null || true' EXIT
        sudo mount -o "compress=$COMPRESS,$DISCARD" "$LOOP" "$MNT"
        sudo btrfs -q subvolume create "$MNT/_work"
        sudo btrfs -q subvolume create "$MNT/other"
        sudo chown -R "$(id -u):$(id -g)" "$MNT/_work" "$MNT/other"
        populate "$MNT/_work"; sync
        echo "version 2 of mod_1" >> "$MNT/_work/proj/src/pkg/mod_1.py"; sync
        mv "$MNT/_work/proj/docs/big.log" "$MNT/_work/proj/docs/renamed.log"; sync
        echo "other subvolume data" > "$MNT/other/keep.txt"; sync
        cp -a "$MNT/_work" "$OUT/src/_work"           # reference copy for comparison
        # the "accident"
        rm -rf "$MNT/_work/proj"; sync
        sudo btrfs -q subvolume delete "$MNT/other"; sync
        echo "after the accident" > "$MNT/_work/new.txt"; sync
        sudo umount "$MNT"; sudo losetup -d "$LOOP"; trap - EXIT
        ;;
    *)
        sed -n '2,10p' "$0"; exit 64 ;;
esac

echo "image:     $IMG"
echo "reference: $OUT/src"
echo "try:       uv run mbkn-btrfs-rescue -d $IMG --db $OUT/index.sqlite scan"
echo "           uv run mbkn-btrfs-rescue -d $IMG --db $OUT/index.sqlite extract"
echo "           uv run mbkn-btrfs-rescue -d $IMG --db $OUT/index.sqlite shell"
