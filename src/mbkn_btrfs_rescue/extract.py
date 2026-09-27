"""Pass 2: parse leaves of one filesystem into item tables, merged across generations."""

from __future__ import annotations

import sqlite3
import sys
import time

from . import ondisk as od
from .checksum import BlockVerifier
from .db import ITEM_TABLES, get_meta, s64, set_meta, u64
from .device import Device

UPSERT_GEN = (
    "ON CONFLICT DO UPDATE SET gen_min=min(gen_min, excluded.gen_min), "
    "gen_max=max(gen_max, excluded.gen_max)"
)


def fsid_summary(con: sqlite3.Connection) -> list[tuple[bytes, int, int, int, int]]:
    """[(fsid, ok_blocks, bad_blocks, min_gen, max_gen)] ordered by ok_blocks desc."""
    return con.execute(
        "SELECT fsid, sum(csum_ok), sum(1 - csum_ok), min(gen), max(gen) FROM nodes "
        "GROUP BY fsid ORDER BY sum(csum_ok) DESC"
    ).fetchall()


def pick_fsid(con: sqlite3.Connection, wanted: str | None) -> bytes:
    rows = fsid_summary(con)
    if not rows:
        raise SystemExit("no tree blocks in the index - run `scan` first")
    if wanted:
        want = bytes.fromhex(wanted.replace("-", ""))
        if not any(r[0] == want for r in rows):
            raise SystemExit(f"fsid {wanted} not present in the index")
        return want
    return rows[0][0]


def node_still_valid(block: bytes, verify, fsid: bytes, gen: int) -> bool:
    """True if `block` still holds the tree block the index recorded (device unchanged)."""
    if len(block) < od.HEADER_SIZE:
        return False
    h = od.parse_header(block)
    if h.fsid != fsid or h.generation != gen:
        return False
    return verify is None or verify(block)


def _stripes(chunk: od.Chunk) -> str:
    return ",".join(f"{d}:{o}" for d, o in chunk.stripes)


STAT_KEYS = ("leaves", "changed", "inodes", "dirents", "extents", "roots", "chunks")


def prepare_extract(
    con: sqlite3.Connection, fsid: bytes, superblocks: list[od.Superblock] | None = None
) -> None:
    """Reset item tables for `fsid` and seed the chunk map from matching superblocks."""
    for t in ITEM_TABLES:
        con.execute(f"DELETE FROM {t}")
    # categories depend on the namespace; per-extent checks only on the filesystem
    con.execute("DELETE FROM file_cat")
    con.execute("DELETE FROM node_mask")
    con.execute("DELETE FROM best_version")
    con.execute("DELETE FROM file_patch")
    con.execute("DELETE FROM current_dirent")
    con.execute("DELETE FROM meta WHERE key = 'current_built'")
    con.execute("DELETE FROM git_blob")
    con.execute("DELETE FROM meta WHERE key LIKE 'classify_%'")
    if get_meta(con, "fsid") != fsid.hex():
        con.execute("DELETE FROM extent_status")
        con.execute("DELETE FROM sector_patch")
        con.execute("DELETE FROM meta WHERE key = 'patch_serial'")
    set_meta(con, "fsid", fsid.hex())
    for sb in superblocks or []:
        if sb.valid_magic and sb.header_fsid == fsid:
            set_meta(con, "devid", sb.devid)
            for ch in od.parse_sys_chunk_array(sb.sys_chunk_array):
                con.execute(
                    "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?)",
                    (
                        s64(ch.logical),
                        sb.generation,
                        ch.length,
                        s64(ch.type),
                        ch.num_stripes,
                        _stripes(ch),
                    ),
                )
    con.commit()


def extract_leaves(
    dev: Device,
    con: sqlite3.Connection,
    fsid: bytes,
    nodesize: int,
    *,
    trees: set[int] | None = None,
    allow_bad: bool = False,
    phys_range: tuple[int, int] | None = None,
    stats: dict | None = None,
    progress: bool = True,
) -> dict:
    """Parse indexed leaves (optionally only those at phys in [lo, hi)) into item tables.

    Idempotent: items are upserted, so leaves may be processed more than once.
    """
    stats = stats if stats is not None else dict.fromkeys(STAT_KEYS, 0)
    where = "WHERE fsid=? AND level=0" + ("" if allow_bad else " AND csum_ok=1")
    params: tuple = (fsid,)
    if phys_range:
        where += " AND phys >= ? AND phys < ?"
        params += phys_range
    # One copy per (bytenr, gen): DUP metadata stores two, prefer a checksum-valid one.
    leaves = []
    prev = None
    for phys, owner, gen, bytenr in con.execute(
        f"SELECT phys, owner, gen, bytenr FROM nodes {where} "
        "ORDER BY bytenr, gen, csum_ok DESC, phys",
        params,
    ):
        if (bytenr, gen) == prev:
            continue
        prev = (bytenr, gen)
        tree = u64(owner)
        if tree in (od.ROOT_TREE, od.CHUNK_TREE) or (
            od.is_fs_tree(tree) and (trees is None or tree in trees)
        ):
            leaves.append((phys, tree, gen))
    leaves.sort(key=lambda r: (r[2], r[0]))

    verify = BlockVerifier(int(get_meta(con, "csum_type", "0")), nodesize)
    t0 = last = time.monotonic()
    for i, (phys, tree, gen) in enumerate(leaves, 1):
        block = dev.pread(nodesize, phys)
        if not node_still_valid(block, None if allow_bad else verify, fsid, gen):
            stats["changed"] += 1
            continue
        _extract_leaf(con, block, nodesize, tree, gen, stats)
        stats["leaves"] += 1
        if i % 2000 == 0:
            con.commit()
        now = time.monotonic()
        if progress and (now - last > 2 or i == len(leaves)):
            last = now
            print(
                f"\r  leaves {i}/{len(leaves)}  inodes {stats['inodes']}  "
                f"dirents {stats['dirents']}  extents {stats['extents']}  "
                f"({now - t0:.0f}s) ",
                end="",
                file=sys.stderr,
                flush=True,
            )
    if progress and leaves:
        print(file=sys.stderr)
    con.commit()
    return stats


def extract(
    dev: Device,
    con: sqlite3.Connection,
    fsid: bytes,
    nodesize: int,
    trees: set[int] | None = None,
    allow_bad: bool = False,
    superblocks: list[od.Superblock] | None = None,
    progress: bool = True,
) -> dict:
    prepare_extract(con, fsid, superblocks)
    return extract_leaves(
        dev, con, fsid, nodesize, trees=trees, allow_bad=allow_bad, progress=progress
    )


def _extract_leaf(
    con: sqlite3.Connection, block: bytes, nodesize: int, tree: int, gen: int, stats: dict
) -> None:
    for it in od.leaf_items(block, nodesize):
        try:
            _extract_item(con, it, tree, gen, stats)
        except Exception:
            continue


def _extract_item(con: sqlite3.Connection, it: od.Item, tree: int, gen: int, stats: dict):
    t = it.type
    if tree == od.CHUNK_TREE:
        if t == od.CHUNK_ITEM:
            ch, _ = od.parse_chunk(it.offset, it.data)
            con.execute(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?)",
                (s64(ch.logical), gen, ch.length, s64(ch.type), ch.num_stripes, _stripes(ch)),
            )
            stats["chunks"] += 1
        return

    if tree == od.ROOT_TREE:
        if t == od.ROOT_ITEM and od.is_fs_tree(it.objectid):
            ri = od.parse_root_item(it.data)
            if ri:
                con.execute(
                    "INSERT OR IGNORE INTO roots VALUES (?,?,?,?,?,?,?,?)",
                    (
                        s64(it.objectid),
                        s64(it.offset),
                        gen,
                        s64(ri.bytenr),
                        ri.level,
                        ri.generation,
                        ri.refs,
                        ri.drop_level,
                    ),
                )
                stats["roots"] += 1
        elif t in (od.ROOT_REF, od.ROOT_BACKREF):
            ref = od.parse_root_ref(it.data)
            if ref:
                child, parent = (
                    (it.offset, it.objectid) if t == od.ROOT_REF else (it.objectid, it.offset)
                )
                con.execute(
                    f"INSERT INTO root_refs VALUES (?,?,?,?,?,?) {UPSERT_GEN}",
                    (s64(child), s64(parent), s64(ref[0]), ref[2], gen, gen),
                )
        return

    ino = s64(it.objectid)
    if t == od.INODE_ITEM:
        ii = od.parse_inode(it.data)
        con.execute(
            "INSERT OR IGNORE INTO inodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s64(tree),
                ino,
                gen,
                ii.generation,
                ii.transid,
                ii.size,
                ii.nbytes,
                ii.mode,
                ii.nlink,
                ii.uid,
                ii.gid,
                ii.atime,
                ii.mtime,
                ii.ctime,
            ),
        )
        stats["inodes"] += 1
    elif t in (od.DIR_ITEM, od.DIR_INDEX):
        for de in od.parse_dir_items(it.data):
            kind = od.ROOT_ITEM if de.child_key_type == od.ROOT_ITEM else od.INODE_ITEM
            _dirent(con, tree, ino, de.name, s64(de.child), kind, de.ftype, gen)
            stats["dirents"] += 1
    elif t == od.INODE_REF:
        for _idx, name in od.parse_inode_refs(it.data):
            _dirent(con, tree, s64(it.offset), name, ino, od.INODE_ITEM, 0, gen)
            stats["dirents"] += 1
    elif t == od.INODE_EXTREF:
        for parent, _idx, name in od.parse_inode_extrefs(it.data):
            _dirent(con, tree, s64(parent), name, ino, od.INODE_ITEM, 0, gen)
            stats["dirents"] += 1
    elif t == od.EXTENT_DATA:
        fe = od.parse_file_extent(it.data)
        if fe is None:
            return
        con.execute(
            "INSERT INTO extents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (tree, ino, foff, egen, etype, comp, disk_bytenr, eoff, nbytes) "
            "DO UPDATE SET gen_min=min(gen_min, excluded.gen_min), "
            "gen_max=max(gen_max, excluded.gen_max)",
            (
                s64(tree),
                ino,
                s64(it.offset),
                fe.generation,
                fe.type,
                fe.compression,
                fe.encryption,
                fe.ram_bytes,
                s64(fe.disk_bytenr),
                fe.disk_num_bytes,
                fe.offset,
                fe.num_bytes,
                fe.inline,
                gen,
                gen,
            ),
        )
        stats["extents"] += 1


def _dirent(con, tree, parent, name, child, kind, ftype, gen):
    con.execute(
        "INSERT INTO dirents VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT DO UPDATE SET ftype=max(ftype, excluded.ftype), "
        "gen_min=min(gen_min, excluded.gen_min), gen_max=max(gen_max, excluded.gen_max)",
        (s64(tree), parent, name, child, kind, ftype, gen, gen),
    )
