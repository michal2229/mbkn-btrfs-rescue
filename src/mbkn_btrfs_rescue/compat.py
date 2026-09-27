"""What this tool supports of the btrfs on-disk format, and warnings when a filesystem differs.

btrfs has no format version number: the on-disk format is stable and extended through feature
flags in the superblock. This module compares those flags (and a few geometry values) with
what the parser, address mapping and verification here handle.
"""

from __future__ import annotations

from . import ondisk as od

# Features whose on-disk effects are handled (or irrelevant: only fs/root/chunk/csum trees are read)
SUPPORTED_INCOMPAT = {
    "mixed_backref",
    "default_subvol",
    "compress_lzo",
    "compress_zstd",
    "big_metadata",
    "extended_iref",
    "skinny_metadata",
    "no_holes",
    "metadata_uuid",
    "raid1c34",  # mirrored copies: any one copy on this device is enough
    "simple_quota",
}
UNSUPPORTED_INCOMPAT = {
    "raid56": "RAID5/6 data is striped with parity across devices; files cannot be reassembled",
    "extent_tree_v2": "experimental format change; tree layout differs, results unreliable",
    "raid_stripe_tree": "logical-to-physical mapping lives in a stripe tree that is not read",
    "zoned": "zoned devices use append-only allocation; untested",
    "mixed_groups": "mixed data+metadata block groups (small filesystems); untested",
}
SUPPORTED_COMPAT_RO = {"free_space_tree", "free_space_tree_valid", "verity", "block_group_tree"}
TESTED_SECTORSIZES = {4096}


def flag_names(flags: int, table: dict[int, str]) -> list[str]:
    names = [name for bit, name in table.items() if flags & bit]
    unknown = flags & ~sum(table)
    if unknown:
        names.append(f"unknown({unknown:#x})")
    return names


def check_superblock(sb: od.Superblock) -> list[str]:
    """Human-readable warnings for anything outside what this tool supports (empty = fine)."""
    out = []
    for name in flag_names(sb.incompat_flags, od.INCOMPAT_FLAGS):
        if name in SUPPORTED_INCOMPAT:
            continue
        reason = UNSUPPORTED_INCOMPAT.get(name, "feature newer than this tool; results unreliable")
        out.append(f"incompat feature '{name}': {reason}")
    for name in flag_names(sb.compat_ro_flags, od.COMPAT_RO_FLAGS):
        if name not in SUPPORTED_COMPAT_RO:
            out.append(f"compat_ro feature '{name}' is unknown to this tool; results may be off")
    if sb.csum_type not in od.CSUM_SIZES:
        out.append(f"unknown checksum type {sb.csum_type}: nothing can be verified")
    if sb.sectorsize not in TESTED_SECTORSIZES:
        out.append(f"sectorsize {sb.sectorsize} is untested (only 4096 is)")
    if sb.num_devices > 1:
        out.append(
            f"multi-device filesystem ({sb.num_devices} devices): only data stored on this "
            "device (devid {}) can be recovered; striped profiles (RAID0/10/5/6) are "
            "unsupported".format(sb.devid)
        )
    return out


def device_warnings(superblocks: list[od.Superblock]) -> list[str]:
    """Warnings for the newest valid superblock; a note when there is none."""
    valid = [sb for sb in superblocks if sb.valid_magic]
    if not valid:
        return [
            "no valid btrfs superblock: filesystem parameters (nodesize, checksum type) are "
            "guessed as 16 KiB / crc32c; pass --fsid or scan options if that is wrong"
        ]
    return check_superblock(max(valid, key=lambda s: s.generation))
