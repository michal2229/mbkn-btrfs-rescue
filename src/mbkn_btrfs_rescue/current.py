"""The filesystem as it is now: what a normal mount of the device would show.

The index merges every generation (deleted and renamed files included). For `current/` in the
mount, the current root tree is walked from the newest superblock; its root items name the
subvolumes that exist and where their trees start. Each of those trees is walked in turn: node
pointers carry the child's generation, and the scan index maps (logical address, generation)
to the block on disk, which is re-read and verified. Directory entries found in those leaves
are exactly the names that exist now; subvolume links place nested subvolumes where they are
mounted.

Blocks of the current trees that no longer verify (overwritten) are counted; the directory
entries they held are missing from current/ (the merged views still have them).
"""

from __future__ import annotations

import sys
import time

from . import ondisk as od
from .db import s64, set_meta
from .model import RescueFS


def _walk(fs: RescueFS, bytenr: int, gen: int, stats: dict[str, int]):
    """Yield the leaves of the tree whose root block is (bytenr, gen); count missing blocks."""
    stack = [(bytenr, gen)]
    while stack:
        b, g = stack.pop()
        block = _block(fs, b, g)
        if block is None:
            stats["missing"] += 1
            continue
        stats["blocks"] += 1
        if od.parse_header(block).level > 0:
            stack.extend((ptr, pg) for _o, _t, _off, ptr, pg in od.node_ptrs(block, fs.nodesize))
        else:
            yield block


def current_roots(fs: RescueFS, stats: dict[str, int]) -> dict[int, tuple[int, int]]:
    """{subvolume tree id: (root block bytenr, generation)} of the subvolumes that exist now.

    Read from the current root tree, which the newest superblock of this filesystem points to.
    Without one (e.g. after a new mkfs), falls back to the index: subvolumes referenced in the
    newest root-tree leaves, each with its newest root item.
    """
    sbs = [sb for sb in fs.dev.superblocks() if sb.valid_magic and sb.header_fsid == fs.fsid]
    if sbs:
        sb = max(sbs, key=lambda x: x.generation)
        roots: dict[int, tuple[int, int]] = {}
        seen_any = False
        for leaf in _walk(fs, sb.root, sb.generation, stats):
            seen_any = True
            for it in od.leaf_items(leaf, fs.nodesize):
                if it.type == od.ROOT_ITEM and od.is_fs_tree(it.objectid):
                    ri = od.parse_root_item(it.data)
                    if ri and ri.refs > 0:
                        roots[it.objectid] = (ri.bytenr, ri.generation)
        if seen_any:
            stats["from_superblock"] = 1
            return roots
    row = fs.con.execute("SELECT max(gen_max) FROM root_refs").fetchone()
    trees = {od.FS_TREE}
    if row and row[0] is not None:
        trees |= {
            c for (c,) in fs.con.execute("SELECT child FROM root_refs WHERE gen_max=?", (row[0],))
        }
    out = {}
    for tree in trees:
        r = fs.con.execute(
            "SELECT bytenr, root_gen FROM roots WHERE tree=? ORDER BY gen DESC LIMIT 1",
            (s64(tree),),
        ).fetchone()
        if r:
            out[tree] = (r[0], r[1])
    return out


def build_current(fs: RescueFS, progress: bool = True) -> dict[str, int]:
    """Walk the current tree of every existing subvolume; store its directory entries."""
    con = fs.con
    t0 = time.monotonic()
    stats = {"trees": 0, "blocks": 0, "missing": 0, "entries": 0, "from_superblock": 0}
    rows: list[tuple] = []
    for tree, (bytenr, gen) in sorted(current_roots(fs, stats).items()):
        stats["trees"] += 1
        for leaf in _walk(fs, bytenr, gen, stats):
            for it in od.leaf_items(leaf, fs.nodesize):
                if it.type != od.DIR_INDEX:
                    continue
                for de in od.parse_dir_items(it.data):
                    rows.append(
                        (tree, s64(it.objectid), de.name, s64(de.child), de.child_key_type,
                         de.ftype)
                    )  # fmt: skip
        if progress:
            print(
                f"\r  current state: {stats['trees']} subvolumes, {stats['blocks']} blocks, "
                f"{len(rows)} names ",
                end="",
                file=sys.stderr,
                flush=True,
            )
    con.execute("DELETE FROM current_dirent")
    con.executemany("INSERT OR REPLACE INTO current_dirent VALUES (?,?,?,?,?,?)", rows)
    stats["entries"] = len(rows)
    set_meta(con, "current_missing", stats["missing"])
    set_meta(con, "current_built", int(time.time()))
    con.commit()
    if progress:
        print(
            f"\r  current state: {stats['trees']} subvolumes, {stats['entries']} names, "
            f"{stats['missing']} unreadable tree blocks ({time.monotonic() - t0:.0f}s)",
            file=sys.stderr,
        )
    return stats


def _block(fs: RescueFS, bytenr: int, gen: int) -> bytes | None:
    """The verified tree block with this logical address and generation (from the index)."""
    for (phys,) in fs.con.execute(
        "SELECT phys FROM nodes WHERE bytenr=? AND gen=? AND fsid=? AND csum_ok=1",
        (s64(bytenr), gen, fs.fsid),
    ):
        block = fs.read_node(phys, gen)
        if block is not None:
            return block
    return None
