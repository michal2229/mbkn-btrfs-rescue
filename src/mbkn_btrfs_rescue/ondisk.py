"""btrfs on-disk format: constants and pure parsers (all little-endian).

Reference: linux/include/uapi/linux/btrfs_tree.h and fs/btrfs/accessors.h.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SUPER_OFFSETS = (0x10000, 0x4000000, 0x4000000000)  # 64 KiB, 64 MiB, 256 GiB
SUPER_MAGIC = b"_BHRfS_M"
HEADER_SIZE = 101
ITEM_SIZE = 25
KEY_PTR_SIZE = 33

# Well-known tree ids (header.owner / root item objectid)
ROOT_TREE = 1
EXTENT_TREE = 2
CHUNK_TREE = 3
DEV_TREE = 4
FS_TREE = 5
CSUM_TREE = 7
FIRST_FREE_OBJECTID = 256
LAST_FREE_OBJECTID = 2**64 - 256
FIRST_CHUNK_TREE_OBJECTID = 256
EXTENT_CSUM_OBJECTID = 2**64 - 10
TREE_NAMES = {
    1: "root",
    2: "extent",
    3: "chunk",
    4: "dev",
    5: "fs(top-level)",
    6: "root-dir",
    7: "csum",
    8: "quota",
    9: "uuid",
    10: "free-space",
    11: "block-group",
    12: "raid-stripe",
    2**64 - 4: "balance",
    2**64 - 5: "orphan",
    2**64 - 6: "tree-log",
    2**64 - 7: "tree-log-fixup",
    2**64 - 8: "tree-reloc",
    2**64 - 9: "data-reloc",
}

# Item key types
INODE_ITEM = 1
INODE_REF = 12
INODE_EXTREF = 13
XATTR_ITEM = 24
DIR_ITEM = 84
DIR_INDEX = 96
EXTENT_DATA = 108
EXTENT_CSUM = 128
ROOT_ITEM = 132
ROOT_BACKREF = 144
ROOT_REF = 156
CHUNK_ITEM = 228

# File extent types / compression
FILE_EXTENT_INLINE = 0
FILE_EXTENT_REG = 1
FILE_EXTENT_PREALLOC = 2
COMPRESS_NAMES = {0: "none", 1: "zlib", 2: "lzo", 3: "zstd"}

# Checksum types
CSUM_CRC32C, CSUM_XXHASH, CSUM_SHA256, CSUM_BLAKE2 = 0, 1, 2, 3
CSUM_SIZES = {CSUM_CRC32C: 4, CSUM_XXHASH: 8, CSUM_SHA256: 32, CSUM_BLAKE2: 32}

# Superblock feature flags (see fs/btrfs/ in the kernel, uapi/linux/btrfs.h)
INCOMPAT_FLAGS = {
    1 << 0: "mixed_backref",
    1 << 1: "default_subvol",
    1 << 2: "mixed_groups",
    1 << 3: "compress_lzo",
    1 << 4: "compress_zstd",
    1 << 5: "big_metadata",
    1 << 6: "extended_iref",
    1 << 7: "raid56",
    1 << 8: "skinny_metadata",
    1 << 9: "no_holes",
    1 << 10: "metadata_uuid",
    1 << 11: "raid1c34",
    1 << 12: "zoned",
    1 << 13: "extent_tree_v2",
    1 << 14: "raid_stripe_tree",
    1 << 16: "simple_quota",
}
COMPAT_RO_FLAGS = {
    1 << 0: "free_space_tree",
    1 << 1: "free_space_tree_valid",
    1 << 2: "verity",
    1 << 3: "block_group_tree",
}
INCOMPAT_METADATA_UUID = 1 << 10

# Block group type bits
BG_DATA, BG_SYSTEM, BG_METADATA = 1, 2, 4
BG_RAID0, BG_RAID1, BG_DUP, BG_RAID10 = 1 << 3, 1 << 4, 1 << 5, 1 << 6
BG_RAID5, BG_RAID6, BG_RAID1C3, BG_RAID1C4 = 1 << 7, 1 << 8, 1 << 9, 1 << 10

# Directory entry file types
FT_NAMES = {0: "?", 1: "file", 2: "dir", 3: "chr", 4: "blk", 5: "fifo", 6: "sock", 7: "symlink"}
FT_REG, FT_DIR, FT_SYMLINK = 1, 2, 7

S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0o170000, 0o040000, 0o100000, 0o120000


def tree_name(tree_id: int) -> str:
    if tree_id in TREE_NAMES:
        return TREE_NAMES[tree_id]
    if FIRST_FREE_OBJECTID <= tree_id <= LAST_FREE_OBJECTID:
        return "subvolume"
    return "?"


def is_fs_tree(tree_id: int) -> bool:
    return tree_id == FS_TREE or FIRST_FREE_OBJECTID <= tree_id <= LAST_FREE_OBJECTID


# --------------------------------------------------------------------------- superblock

_SB = struct.Struct("<32s16sQQ8sQQQQQQQQQIIIIIQQQQHBBB")
_BACKUP = struct.Struct("<" + "Q" * 19 + "BBBBBB10x")


@dataclass
class BackupRoot:
    tree_root: int
    tree_root_gen: int
    chunk_root: int
    chunk_root_gen: int
    fs_root: int
    fs_root_gen: int
    csum_root: int
    csum_root_gen: int


@dataclass
class Superblock:
    offset: int
    csum: bytes
    fsid: bytes
    bytenr: int
    magic: bytes
    generation: int
    root: int
    chunk_root: int
    total_bytes: int
    bytes_used: int
    num_devices: int
    sectorsize: int
    nodesize: int
    sys_chunk_array_size: int
    chunk_root_generation: int
    incompat_flags: int
    compat_ro_flags: int
    devid: int
    csum_type: int
    root_level: int
    chunk_root_level: int
    label: str
    metadata_uuid: bytes
    sys_chunk_array: bytes
    backups: list[BackupRoot]
    raw: bytes

    @property
    def valid_magic(self) -> bool:
        return self.magic == SUPER_MAGIC

    @property
    def header_fsid(self) -> bytes:
        """The UUID stamped into tree block headers."""
        if self.incompat_flags & INCOMPAT_METADATA_UUID:
            return self.metadata_uuid
        return self.fsid


def parse_superblock(raw: bytes, offset: int) -> Superblock:
    f = _SB.unpack_from(raw, 0)
    (
        csum,
        fsid,
        bytenr,
        _flags,
        magic,
        generation,
        root,
        chunk_root,
        _log_root,
        _lrt,
        total_bytes,
        bytes_used,
        _root_dir,
        num_devices,
        sectorsize,
        nodesize,
        _leafsize,
        _stripesize,
        sys_size,
        chunk_root_gen,
        _compat,
        compat_ro,
        incompat,
        csum_type,
        root_level,
        chunk_level,
        _log_level,
    ) = f
    label = raw[299 : 299 + 256].split(b"\0", 1)[0].decode("utf-8", "replace")
    metadata_uuid = raw[571:587]
    sys_array = raw[811 : 811 + min(sys_size, 2048)]
    backups = []
    for i in range(4):
        b = _BACKUP.unpack_from(raw, 2859 + i * 168)
        backups.append(BackupRoot(b[0], b[1], b[2], b[3], b[6], b[7], b[10], b[11]))
    return Superblock(
        offset,
        csum,
        fsid,
        bytenr,
        magic,
        generation,
        root,
        chunk_root,
        total_bytes,
        bytes_used,
        num_devices,
        sectorsize,
        nodesize,
        sys_size,
        chunk_root_gen,
        incompat,
        compat_ro,
        int.from_bytes(raw[201:209], "little"),  # dev_item.devid
        csum_type,
        root_level,
        chunk_level,
        label,
        metadata_uuid,
        sys_array,
        backups,
        raw,
    )


# --------------------------------------------------------------------------- tree blocks

_HEADER = struct.Struct("<32s16sQQ16sQQIB")
_KEY = struct.Struct("<QBQ")
_ITEM = struct.Struct("<QBQII")
_KEY_PTR = struct.Struct("<QBQQQ")


@dataclass(slots=True)
class Header:
    csum: bytes
    fsid: bytes
    bytenr: int
    flags: int
    generation: int
    owner: int
    nritems: int
    level: int


def parse_header(block: bytes | memoryview) -> Header:
    csum, fsid, bytenr, flags, _ctu, gen, owner, nritems, level = _HEADER.unpack_from(block, 0)
    return Header(csum, bytes(fsid), bytenr, flags, gen, owner, nritems, level)


@dataclass(slots=True)
class Item:
    objectid: int
    type: int
    offset: int
    data: bytes


def leaf_items(block: bytes, nodesize: int) -> list[Item]:
    """Parse all items of a leaf, skipping any whose data range is out of bounds."""
    nritems = struct.unpack_from("<I", block, 96)[0]
    items = []
    max_items = (nodesize - HEADER_SIZE) // ITEM_SIZE
    for i in range(min(nritems, max_items)):
        obj, typ, off, doff, dsize = _ITEM.unpack_from(block, HEADER_SIZE + i * ITEM_SIZE)
        start = HEADER_SIZE + doff
        if start + dsize > nodesize:
            continue
        items.append(Item(obj, typ, off, block[start : start + dsize]))
    return items


def first_last_keys(block: bytes, nodesize: int, level: int, nritems: int):
    """Return ((obj,type,off) of first, (obj,type,off) of last) key, or None."""
    if nritems == 0:
        return None
    stride, cap = (
        (ITEM_SIZE, (nodesize - HEADER_SIZE) // ITEM_SIZE)
        if level == 0
        else (KEY_PTR_SIZE, (nodesize - HEADER_SIZE) // KEY_PTR_SIZE)
    )
    n = min(nritems, cap)
    first = _KEY.unpack_from(block, HEADER_SIZE)
    last = _KEY.unpack_from(block, HEADER_SIZE + (n - 1) * stride)
    return first, last


def node_ptrs(block: bytes, nodesize: int) -> list[tuple[int, int, int, int, int]]:
    """(objectid, type, offset, blockptr, generation) for each pointer in an internal node."""
    nritems = struct.unpack_from("<I", block, 96)[0]
    cap = (nodesize - HEADER_SIZE) // KEY_PTR_SIZE
    return [
        _KEY_PTR.unpack_from(block, HEADER_SIZE + i * KEY_PTR_SIZE)
        for i in range(min(nritems, cap))
    ]


# --------------------------------------------------------------------------- item payloads

_INODE = struct.Struct("<QQQQQIIIIQQQ32xQIQIQIQI")


@dataclass(slots=True)
class InodeItem:
    generation: int
    transid: int
    size: int
    nbytes: int
    nlink: int
    uid: int
    gid: int
    mode: int
    flags: int
    atime: float
    ctime: float
    mtime: float
    otime: float


def parse_inode(data: bytes) -> InodeItem:
    (
        gen,
        transid,
        size,
        nbytes,
        _bg,
        nlink,
        uid,
        gid,
        mode,
        _rdev,
        flags,
        _seq,
        at,
        atn,
        ct,
        ctn,
        mt,
        mtn,
        ot,
        otn,
    ) = _INODE.unpack_from(data, 0)
    return InodeItem(
        gen,
        transid,
        size,
        nbytes,
        nlink,
        uid,
        gid,
        mode,
        flags,
        at + atn / 1e9,
        ct + ctn / 1e9,
        mt + mtn / 1e9,
        ot + otn / 1e9,
    )


def parse_inode_refs(data: bytes) -> list[tuple[int, bytes]]:
    """INODE_REF payload -> [(dir_index, name)]; parent is the key offset."""
    out, pos = [], 0
    while pos + 10 <= len(data):
        index, nlen = struct.unpack_from("<QH", data, pos)
        pos += 10
        out.append((index, data[pos : pos + nlen]))
        pos += nlen
    return out


def parse_inode_extrefs(data: bytes) -> list[tuple[int, int, bytes]]:
    """INODE_EXTREF payload -> [(parent, dir_index, name)]."""
    out, pos = [], 0
    while pos + 18 <= len(data):
        parent, index, nlen = struct.unpack_from("<QQH", data, pos)
        pos += 18
        out.append((parent, index, data[pos : pos + nlen]))
        pos += nlen
    return out


_DIR = struct.Struct("<QBQQHHB")


@dataclass(slots=True)
class DirEntry:
    child: int
    child_key_type: int  # INODE_ITEM for inodes, ROOT_ITEM for subvolume links
    transid: int
    ftype: int
    name: bytes


def parse_dir_items(data: bytes) -> list[DirEntry]:
    out, pos = [], 0
    while pos + 30 <= len(data):
        obj, ktype, _koff, transid, dlen, nlen, ftype = _DIR.unpack_from(data, pos)
        pos += 30
        out.append(DirEntry(obj, ktype, transid, ftype, data[pos : pos + nlen]))
        pos += nlen + dlen
    return out


_FE = struct.Struct("<QQBBHB")
_FE_REG = struct.Struct("<QQQQ")


@dataclass(slots=True)
class FileExtent:
    generation: int
    ram_bytes: int
    compression: int
    encryption: int
    type: int
    disk_bytenr: int = 0
    disk_num_bytes: int = 0
    offset: int = 0
    num_bytes: int = 0
    inline: bytes | None = None


def parse_file_extent(data: bytes) -> FileExtent | None:
    if len(data) < 21:
        return None
    gen, ram, comp, enc, _other, typ = _FE.unpack_from(data, 0)
    fe = FileExtent(gen, ram, comp, enc, typ)
    if typ == FILE_EXTENT_INLINE:
        fe.inline = bytes(data[21:])
        fe.num_bytes = ram
    elif len(data) >= 53:
        fe.disk_bytenr, fe.disk_num_bytes, fe.offset, fe.num_bytes = _FE_REG.unpack_from(data, 21)
    else:
        return None
    return fe


_CHUNK = struct.Struct("<QQQQIIIHH")
_STRIPE = struct.Struct("<QQ16s")


@dataclass(slots=True)
class Chunk:
    logical: int
    length: int
    type: int
    num_stripes: int
    stripes: list[tuple[int, int]]  # (devid, physical offset)


def parse_chunk(logical: int, data: bytes | memoryview) -> tuple[Chunk, int]:
    """Parse a chunk item; returns (chunk, bytes consumed)."""
    length, _owner, _stripe_len, typ, _ia, _iw, _ss, nstripes, _sub = _CHUNK.unpack_from(data, 0)
    stripes = []
    for i in range(nstripes):
        if 48 + (i + 1) * 32 > len(data):  # corrupt count: keep the stripes that are there
            break
        devid, off, _uuid = _STRIPE.unpack_from(data, 48 + i * 32)
        stripes.append((devid, off))
    return Chunk(logical, length, typ, nstripes, stripes), 48 + nstripes * 32


def parse_sys_chunk_array(array: bytes) -> list[Chunk]:
    out, pos = [], 0
    while pos + 17 <= len(array):
        _obj, typ, off = _KEY.unpack_from(array, pos)
        pos += 17
        if typ != CHUNK_ITEM:
            break
        chunk, used = parse_chunk(off, array[pos:])
        out.append(chunk)
        pos += used
    return out


_ROOT = struct.Struct("<QQQQQQQI17sBB")


@dataclass(slots=True)
class RootItem:
    generation: int
    root_dirid: int
    bytenr: int
    last_snapshot: int
    flags: int
    refs: int
    drop_level: int
    level: int


def parse_root_item(data: bytes) -> RootItem | None:
    if len(data) < 239:
        return None
    gen, dirid, bytenr, _limit, _used, last_snap, flags, refs, _drop, drop_level, level = (
        _ROOT.unpack_from(data, 160)
    )
    return RootItem(gen, dirid, bytenr, last_snap, flags, refs, drop_level, level)


def parse_root_ref(data: bytes) -> tuple[int, int, bytes] | None:
    """ROOT_REF / ROOT_BACKREF payload -> (dirid, sequence, name)."""
    if len(data) < 18:
        return None
    dirid, seq, nlen = struct.unpack_from("<QQH", data, 0)
    return dirid, seq, data[18 : 18 + nlen]
