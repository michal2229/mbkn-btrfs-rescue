"""Browsable view over the extracted index.

Namespace layout (all read-only):

    /                         one entry per filesystem tree: "<name>@<tree id>"
    /<name>@<id>/...          directory tree of that subvolume (root inode 256)
    /<name>@<id>/.orphans/    inodes not reachable from the subvolume root

Items are merged across generations: every name ever seen for a directory is listed (newest
wins on name clashes), which surfaces deleted and renamed files too. File content is assembled
by "painting" file extent items in the order they were written (extent generation), so newer
writes cover older ones. `at_gen` limits the view to items seen at or before a generation,
which gives older versions of files.
"""

from __future__ import annotations

import bisect
import copy
import json
import os
import sqlite3
import stat as stat_mod
import zlib
from collections import OrderedDict, defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
from dissect.btrfs.stream import decode_extent

from . import ondisk as od
from .checksum import BlockVerifier, csum_function
from .db import get_meta
from .device import Device
from .extract import node_still_valid
from .sniff import SNIFF_BYTES, plausible

ROOT_INO = 256
KIND_ROOT, KIND_ORPHANS, KIND_INODE = "root", "orphans", "inode"
ORPHANS_NAME = ".orphans"

CAT_INTACT, CAT_UNVERIFIED, CAT_DAMAGED, CAT_LOST = "intact", "unverified", "damaged", "lost"
CATEGORIES = (CAT_INTACT, CAT_UNVERIFIED, CAT_DAMAGED, CAT_LOST)
CAT_BITS = {c: 1 << i for i, c in enumerate(CATEGORIES)}
CAT_RANK = {c: i for i, c in enumerate(CATEGORIES)}  # lower = better content
GOOD_CATS = (CAT_INTACT, CAT_UNVERIFIED)
BEST_BIT = 1 << len(CATEGORIES)  # mask bit: something readable in the best/ view
MASK_VERSION = 7  # bump when mask semantics change (forces re-classification)
MAX_VERSIONS = 64  # older versions tried per file when looking for better content
CAT_HELP = {
    CAT_INTACT: "content verified against btrfs checksums (or stored inline in metadata)",
    CAT_UNVERIFIED: "no checksum on record, but the data looks plausible - probably fine, check",
    CAT_DAMAGED: "some sectors verify, some do not - partially readable",
    CAT_LOST: "no sector verifies - space was reused or discarded (TRIM); names/metadata only",
}


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Node:
    tree: int
    ino: int
    kind: str = KIND_INODE


ROOT = Node(0, 0, KIND_ROOT)


@dataclass
class Entry:
    name: str
    node: Node
    ftype: int
    gen_min: int = 0
    gen_max: int = 0
    stale: bool = False  # the inode's newest known name/location is somewhere else

    @property
    def is_dir(self) -> bool:
        return self.ftype == od.FT_DIR


@dataclass
class InodeInfo:
    gen: int
    created: int
    transid: int
    size: int
    nbytes: int
    mode: int
    nlink: int
    uid: int
    gid: int
    atime: float
    mtime: float
    ctime: float


@dataclass
class ExtentRow:
    foff: int
    egen: int
    etype: int
    comp: int
    enc: int
    ram_bytes: int
    disk_bytenr: int
    disk_len: int
    eoff: int
    nbytes: int
    inline: bytes | None
    gen_min: int
    gen_max: int

    @property
    def length(self) -> int:
        if self.etype == od.FILE_EXTENT_INLINE:
            return self.ram_bytes
        return self.nbytes

    @property
    def is_data(self) -> bool:
        return self.etype == od.FILE_EXTENT_REG and self.disk_bytenr != 0


SECTOR_OK, SECTOR_BAD, SECTOR_NOCSUM = 0, 1, 2
# Bump when check_extent's rules change; stored results of older rules are re-checked.
# 2: per-sector states; sectors without a checksum that read as all zeros count as bad.
# 3: unchecksummed compressed extents that fail to decompress count as bad.
CHECK_VERSION = 3
# Kinds of sector patches (see match.py): read the sector from a confirmed identical copy
# elsewhere on the device, or as zeros (its expected checksum is that of an all-zero sector).
PATCH_NONE, PATCH_COPY, PATCH_ZERO = 0, 1, 3


class ExtentCheck(NamedTuple):
    good: int
    bad: int
    nocsum: int
    zero: int
    mapped: int
    sectors: bytes | None = None  # zlib-compressed per-sector states when not uniform
    copied: int = 0  # bad sectors replaced from copies (not stored: patches are applied on load)

    @property
    def uniform(self) -> bool:
        return (self.good > 0) + (self.bad > 0) + (self.nocsum > 0) <= 1

    def counts(self, ranges: list[tuple[int, int]], nsect: int) -> tuple[int, int, int]:
        """(good, bad, nocsum) among the sectors in `ranges` (a file's referenced part)."""
        mask = np.zeros(nsect, dtype=bool)
        for lo, hi in ranges:
            mask[max(0, lo) : min(nsect, hi)] = True
        n = int(mask.sum())
        if n == nsect:
            return self.good, self.bad, self.nocsum
        if self.uniform:
            return (n, 0, 0) if self.good else (0, n, 0) if self.bad else (0, 0, n)
        if self.sectors is None:  # sampled or legacy result: whole-extent counts
            return self.good, self.bad, self.nocsum
        states = np.frombuffer(zlib.decompress(self.sectors), dtype=np.uint8)[mask]
        c = np.bincount(states, minlength=3)
        return int(c[SECTOR_OK]), int(c[SECTOR_BAD]), int(c[SECTOR_NOCSUM])


@dataclass
class Layout:
    size: int
    segments: list[tuple[int, int, ExtentRow]] = field(default_factory=list)  # [start, end)


class _LRU(OrderedDict):
    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.cap:
            self.popitem(last=False)


def fsname(raw: bytes) -> str:
    return os.fsdecode(raw)


class RescueFS:
    def __init__(
        self,
        con: sqlite3.Connection,
        dev: Device,
        exclude: list[str] | None = None,
        at_gen: int | None = None,
    ):
        self.con = con
        self.dev = dev
        self.exclude = set(exclude or [])
        self.at_gen = at_gen
        self.nodesize = int(get_meta(con, "nodesize", "16384"))
        self.sectorsize = int(get_meta(con, "sectorsize", "4096"))
        self.csum_type = int(get_meta(con, "csum_type", "0"))
        devid = get_meta(con, "devid")
        self.devid = int(devid) if devid else None
        fsid = get_meta(con, "fsid")
        if fsid is None:
            raise SystemExit("index has no extracted filesystem - run `extract` first")
        self.fsid = bytes.fromhex(fsid)
        self._csum = csum_function(self.csum_type)
        self._verify_block = BlockVerifier(self.csum_type, self.nodesize)
        self._csum_size = od.CSUM_SIZES[self.csum_type]
        self._load_chunks()
        self._layouts = _LRU(8192)
        self._children = _LRU(4096)
        self._trees: list[tuple[int, str, int]] | None = None
        self._decoded = _LRU(64)
        self._orphans: dict[int, list[Entry]] = {}
        self._csum_leaves = _LRU(512)
        self._csum_index: _CsumIndex | None = None
        self._ext_status: dict[tuple[int, int, int], ExtentCheck] | None = None
        self._patch_rows: dict[tuple[int, int, int], bytes] | None = None
        self._patch_arrays = _LRU(256)
        self._cats: dict[tuple, tuple[str, str | None]] = {}  # detail None = not computed
        self._file_stats: dict[tuple, tuple[int, float]] = {}  # size, fraction of bad sectors
        self._best: dict[tuple, tuple[int | None, str]] = {}
        self._best_stored = False
        self._gen_views = _LRU(64)
        self._masks: dict[tuple[int, int], int] | None = None
        self._masks_valid_for: frozenset[str] | None = None
        self._masks_valid = False

    def at(self, gen: int | None) -> RescueFS:
        """A view of the same index as of generation `gen` (shares device, db, data caches)."""
        view = copy.copy(self)
        view.at_gen = gen
        view.exclude = self.exclude
        view._layouts = _LRU(8192)
        view._children = _LRU(4096)
        view._orphans = self._orphans  # orphan detection ignores at_gen
        view._masks = None
        view._best_stored = False
        return view

    def view_at(self, gen: int | None) -> RescueFS:
        """Cached `at(gen)` (None = this view)."""
        if gen is None:
            return self
        if gen not in self._gen_views:
            self._gen_views.put(gen, self.at(gen))
        return self._gen_views[gen]

    def generations(self) -> list[int]:
        """Leaf generations in which any file/directory item was seen, ascending."""
        return [
            g
            for (g,) in self.con.execute(
                "SELECT gen FROM inodes UNION SELECT gen_min FROM dirents "
                "UNION SELECT gen_min FROM extents ORDER BY 1"
            )
        ]

    # ------------------------------------------------------------------ address mapping

    def _load_chunks(self) -> None:
        best: dict[int, tuple] = {}
        for logical, gen, length, typ, stripes in self.con.execute(
            "SELECT logical, gen, length, type, stripes FROM chunks ORDER BY gen"
        ):
            best[logical] = (
                length,
                typ,
                [tuple(map(int, s.split(":"))) for s in stripes.split(",") if s],
            )
            del gen
        self._chunk_starts = sorted(best)
        self._chunks = [best[s] for s in self._chunk_starts]

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def map_logical(self, logical: int) -> list[int]:
        i = bisect.bisect_right(self._chunk_starts, logical) - 1
        if i < 0:
            return []
        start = self._chunk_starts[i]
        length, typ, stripes = self._chunks[i]
        if logical >= start + length:
            return []
        if typ & (od.BG_RAID0 | od.BG_RAID10 | od.BG_RAID5 | od.BG_RAID6):
            raise DataError("striped RAID profiles are not supported")
        mine = [off for dev, off in stripes if self.devid is None or dev == self.devid]
        if not mine:
            raise DataError(f"logical {logical:#x} is stored on another device only")
        return [p + (logical - start) for p in mine]

    def read_logical(self, logical: int, length: int) -> bytes:
        phys = self.map_logical(logical)
        if not phys:
            raise DataError(f"logical {logical:#x} is not covered by any known chunk")
        return self.dev.pread(length, phys[0])

    def read_node(self, phys: int, gen: int) -> bytes | None:
        """Re-read an indexed tree block; None if the device content changed since the scan."""
        block = self.dev.pread(self.nodesize, phys)
        if not node_still_valid(block, self._verify_block, self.fsid, gen):
            return None
        return block

    # ------------------------------------------------------------------ trees / names

    def trees(self) -> list[tuple[int, str, int]]:
        """[(tree id, name, inode count)] for every filesystem tree in the index (cached)."""
        if self._trees is not None:
            return self._trees
        counts = dict(
            self.con.execute("SELECT tree, count(DISTINCT ino) FROM inodes GROUP BY tree")
        )
        for (t,) in self.con.execute("SELECT DISTINCT tree FROM dirents"):
            counts.setdefault(t, 0)
        names = {}
        for child, name in self.con.execute("SELECT child, name FROM root_refs ORDER BY gen_max"):
            names[child] = fsname(name)
        out = []
        for t in sorted(counts):
            name = "top" if t == od.FS_TREE else names.get(t, "tree")
            out.append((t, name, counts[t]))
        self._trees = out
        return out

    def tree_label(self, tree: int) -> str:
        for t, name, _ in self.trees():
            if t == tree:
                return f"{name}@{t}"
        return f"tree@{tree}"

    # ------------------------------------------------------------------ directory listing

    def _gen_clause(self, col: str = "gen_min") -> tuple[str, tuple]:
        if self.at_gen is None:
            return "", ()
        return f" AND {col} <= ?", (self.at_gen,)

    def children(self, node: Node) -> dict[str, Entry]:
        """{name: entry} of a directory (cached per view; the index is immutable per view)."""
        hit = self._children.get(node)
        if hit is None:
            hit = self._list_children(node)
            self._children.put(node, hit)
        return hit

    def _list_children(self, node: Node) -> dict[str, Entry]:
        if node.kind == KIND_ROOT:
            return {
                f"{name}@{t}": Entry(f"{name}@{t}", Node(t, ROOT_INO), od.FT_DIR)
                for t, name, _ in self.trees()
            }
        if node.kind == KIND_ORPHANS:
            return {e.name: e for e in self.orphans(node.tree)}
        clause, args = self._gen_clause()
        rows = self.con.execute(
            "SELECT name, child, child_kind, ftype, gen_min, gen_max FROM dirents "
            "WHERE tree=? AND dir=?" + clause,
            (node.tree, node.ino, *args),
        ).fetchall()
        out: dict[str, Entry] = {}
        for name_b, child, kind, ftype, gmin, gmax in rows:
            name = fsname(name_b)
            if name in self.exclude or name in ("", ".", "..") or "/" in name:
                continue
            prev = out.get(name)
            if prev and prev.gen_max >= gmax:
                continue
            if kind == od.ROOT_ITEM:
                # nested subvolume: listed once, at the top level as <name>@<id>; following
                # the link here too would show (and walk, and count) its content twice
                continue
            out[name] = Entry(name, Node(node.tree, child), ftype, gmin, gmax)
        inode_entries = [e for e in out.values() if e.node.tree == node.tree]
        self._fill_types(node.tree, inode_entries)
        self._mark_stale(node, inode_entries)
        if node.ino == ROOT_INO and self.orphans(node.tree):
            out[ORPHANS_NAME] = Entry(ORPHANS_NAME, Node(node.tree, 0, KIND_ORPHANS), od.FT_DIR)
        return out

    def _fill_types(self, tree: int, entries: list[Entry]) -> None:
        unknown = [e for e in entries if e.ftype not in od.FT_NAMES or e.ftype == 0]
        for e in unknown:
            info = self.inode(tree, e.node.ino)
            if info:
                e.ftype = _ftype_from_mode(info.mode)
            elif self.con.execute(
                "SELECT 1 FROM dirents WHERE tree=? AND dir=? LIMIT 1", (tree, e.node.ino)
            ).fetchone():
                e.ftype = od.FT_DIR
            else:
                e.ftype = od.FT_REG

    def _mark_stale(self, parent: Node, entries: list[Entry]) -> None:
        if not entries:
            return
        by_ino = {e.node.ino: e for e in entries}
        marks = ",".join("?" * len(by_ino))
        clause, args = self._gen_clause()
        best: dict[int, tuple[int, int, bytes]] = {}
        for child, d, name, gmax in self.con.execute(
            f"SELECT child, dir, name, gen_max FROM dirents WHERE tree=? AND child_kind=1 "
            f"AND child IN ({marks})" + clause,
            (parent.tree, *by_ino, *args),
        ):
            if child not in best or gmax > best[child][0]:
                best[child] = (gmax, d, name)
        for ino, e in by_ino.items():
            if ino in best:
                _, d, name = best[ino]
                e.stale = not (d == parent.ino and fsname(name) == e.name)

    def orphans(self, tree: int) -> list[Entry]:
        if tree in self._orphans:
            return self._orphans[tree]
        parents: dict[int, set[int]] = defaultdict(set)
        kids: dict[int, set[int]] = defaultdict(set)
        names: dict[int, tuple[int, bytes]] = {}
        for d, child, name, gmax in self.con.execute(
            "SELECT dir, child, name, gen_max FROM dirents WHERE tree=? AND child_kind=1", (tree,)
        ):
            parents[child].add(d)
            kids[d].add(child)
            if child not in names or gmax > names[child][0]:
                names[child] = (gmax, name)
        cands = {
            i
            for (i,) in self.con.execute(
                "SELECT DISTINCT ino FROM inodes WHERE tree=? UNION "
                "SELECT DISTINCT ino FROM extents WHERE tree=?",
                (tree, tree),
            )
        }
        cands |= set(parents)
        cands.discard(ROOT_INO)

        def bfs(starts: list[int], seen: set[int]) -> None:
            q = deque(starts)
            seen.update(starts)
            while q:
                for c in kids.get(q.popleft(), ()):
                    if c not in seen:
                        seen.add(c)
                        q.append(c)

        reach: set[int] = set()
        bfs([ROOT_INO], reach)
        lost = cands - reach
        roots = sorted(i for i in lost if not (parents.get(i, set()) & lost))
        covered: set[int] = set()
        bfs(roots, covered)
        for i in sorted(lost - covered):  # cycles (e.g. moved directories)
            if i not in covered:
                roots.append(i)
                bfs([i], covered)
        entries = []
        for ino in roots:
            label = f"{ino}_{fsname(names[ino][1])}" if ino in names else str(ino)
            if names.get(ino) and fsname(names[ino][1]) in self.exclude:
                continue
            e = Entry(label, Node(tree, ino), 0)
            entries.append(e)
        self._fill_types(tree, entries)
        self._orphans[tree] = entries
        return entries

    # ------------------------------------------------------------------ path resolution

    def resolve(self, path: str, cwd: tuple[Entry, ...] = ()) -> tuple[Entry, ...]:
        """Resolve a path to a chain of entries from the root (empty chain = root)."""
        chain = [] if path.startswith("/") else list(cwd)
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if chain:
                    chain.pop()
                continue
            node = chain[-1].node if chain else ROOT
            kids = self.children(node)
            if part not in kids:
                raise FileNotFoundError("/" + "/".join([e.name for e in chain] + [part]))
            chain.append(kids[part])
        return tuple(chain)

    def path_of(self, tree: int, ino: int) -> str | None:
        """Namespace path of an inode by its newest name (None if not under the root)."""
        parts: list[str] = []
        seen: set[int] = set()
        while ino != ROOT_INO:
            if ino in seen:
                return None
            seen.add(ino)
            clause, args = self._gen_clause()
            row = self.con.execute(
                "SELECT dir, name FROM dirents WHERE tree=? AND child=? AND child_kind=1"
                + clause
                + " ORDER BY gen_max DESC LIMIT 1",
                (tree, ino, *args),
            ).fetchone()
            if row is None:
                return None
            ino = row[0]
            parts.append(fsname(row[1]))
        return "/" + "/".join([self.tree_label(tree), *reversed(parts)])

    @staticmethod
    def chain_path(chain: tuple[Entry, ...]) -> str:
        return "/" + "/".join(e.name for e in chain)

    def walk(
        self, entry: Entry, prefix: str = "", include_stale: bool = False
    ) -> Iterator[tuple[str, Entry]]:
        """Depth-first walk yielding (relative path, entry); guards against cycles.

        Stale names (inode renamed/moved elsewhere later) are skipped unless include_stale.
        """
        seen: set[Node] = set()
        stack = [(prefix or entry.name, entry)]
        while stack:
            rel, e = stack.pop()
            yield rel, e
            if e.is_dir and e.node not in seen:
                seen.add(e.node)
                for name, child in sorted(self.children(e.node).items(), reverse=True):
                    if child.node.kind == KIND_ORPHANS:
                        continue
                    if include_stale or not child.stale:
                        stack.append((f"{rel}/{name}", child))

    # ------------------------------------------------------------------ inodes

    def inode(self, tree: int, ino: int) -> InodeInfo | None:
        clause, args = ("", ()) if self.at_gen is None else (" AND gen <= ?", (self.at_gen,))
        row = self.con.execute(
            "SELECT gen, created, transid, size, nbytes, mode, nlink, uid, gid, atime, mtime, "
            "ctime FROM inodes WHERE tree=? AND ino=?" + clause + " ORDER BY gen DESC LIMIT 1",
            (tree, ino, *args),
        ).fetchone()
        return InodeInfo(*row) if row else None

    def versions(self, tree: int, ino: int) -> list[InodeInfo]:
        """All distinct inode states seen (by transid), oldest first."""
        out: dict[int, InodeInfo] = {}
        for row in self.con.execute(
            "SELECT gen, created, transid, size, nbytes, mode, nlink, uid, gid, atime, "
            "mtime, ctime FROM inodes WHERE tree=? AND ino=? ORDER BY gen",
            (tree, ino),
        ):
            info = InodeInfo(*row)
            out.setdefault(info.transid, info)
        return list(out.values())

    def extent_rows(self, tree: int, ino: int) -> list[ExtentRow]:
        """File extent items in painting order: later rows cover earlier ones.

        Order: the newest metadata generation the item was seen in (capped at this view's
        generation) - the item in the newest leaf describes the latest state of that range,
        even when its data is older (reflink copies, dedupe, defrag reuse old extents) - then
        extent generation, then written data over holes/preallocated space from the same
        transaction (fallocate + write turns a prealloc item into a data item).
        """
        if self.at_gen is None:
            seen, clause, args = "gen_max", "", ()
        else:
            seen = "min(gen_max, ?)"
            clause, args = " AND egen <= ? AND gen_min <= ?", (self.at_gen, self.at_gen)
        return [
            ExtentRow(*r)
            for r in self.con.execute(
                "SELECT foff, egen, etype, comp, enc, ram_bytes, disk_bytenr, disk_len, eoff, "
                "nbytes, inline, gen_min, gen_max FROM extents WHERE tree=? AND ino=?"
                + clause
                + f" ORDER BY {seen}, egen, (etype != 2 AND (etype = 0 OR disk_bytenr != 0)), foff",
                (tree, ino, *args, *(() if self.at_gen is None else (self.at_gen,))),
            )
        ]

    def stat(self, entry: Entry) -> dict:
        node = entry.node
        base = {
            "st_nlink": 1,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
            "st_size": 0,
            "st_mtime": 0.0,
            "st_atime": 0.0,
            "st_ctime": 0.0,
        }
        if node.kind != KIND_INODE:
            return base | {"st_mode": stat_mod.S_IFDIR | 0o555, "st_nlink": 2}
        info = self.inode(node.tree, node.ino)
        if entry.is_dir:
            mode = stat_mod.S_IFDIR | ((info.mode & 0o777) if info else 0o755)
            return base | {
                "st_mode": mode | 0o500,
                "st_nlink": 2,
                "st_mtime": info.mtime if info else 0.0,
                "st_ctime": info.ctime if info else 0.0,
                "st_atime": info.atime if info else 0.0,
            }
        layout = self.layout(node.tree, node.ino)
        if entry.ftype == od.FT_SYMLINK:
            mode = stat_mod.S_IFLNK | 0o777
        else:
            mode = stat_mod.S_IFREG | (((info.mode & 0o777) if info else 0o644) | 0o400)
        return base | {
            "st_mode": mode,
            "st_size": layout.size,
            "st_mtime": info.mtime if info else 0.0,
            "st_ctime": info.ctime if info else 0.0,
            "st_atime": info.atime if info else 0.0,
        }

    # ------------------------------------------------------------------ file content

    def layout(self, tree: int, ino: int) -> Layout:
        key = (tree, ino, self.at_gen)
        if key in self._layouts:
            return self._layouts[key]
        segs = paint(self.extent_rows(tree, ino))
        info = self.inode(tree, ino)
        end = segs[-1][1] if segs else 0
        size = info.size if info else end
        segs = [(a, min(b, size), r) for a, b, r in segs if a < size]
        lay = Layout(size, segs)
        self._layouts.put(key, lay)
        return lay

    def _decoded_extent(self, row: ExtentRow) -> bytes:
        key = (row.disk_bytenr, row.disk_len, row.comp, row.egen, row.inline)
        if key in self._decoded:
            return self._decoded[key]
        raw = (
            row.inline
            if row.etype == od.FILE_EXTENT_INLINE
            else self._read_extent(row.disk_bytenr, row.disk_len, row.egen, 0, row.disk_len)
        )
        data = self._decompress(raw, row.comp, row.enc)[: row.ram_bytes] if row.comp else raw
        self._decoded.put(key, data)
        return data

    def _decompress(self, raw: bytes, comp: int, enc: int = 0) -> bytes:
        if comp == 2 and len(raw) >= 4:  # LZO: trust only the framed length
            total = int.from_bytes(raw[:4], "little")
            raw = raw[:total] if 4 <= total <= len(raw) else raw
        try:
            return decode_extent(raw, comp, enc, self.sectorsize)
        except Exception as err:
            raise DataError(f"{od.COMPRESS_NAMES.get(comp)} decode failed: {err}") from err

    def _segment_bytes(self, row: ExtentRow, rel: int, length: int) -> bytes:
        if row.etype == od.FILE_EXTENT_INLINE:
            data = self._decoded_extent(row)[rel : rel + length]
        elif row.etype == od.FILE_EXTENT_PREALLOC or row.disk_bytenr == 0:
            return bytes(length)
        elif row.comp:
            data = self._decoded_extent(row)[row.eoff + rel : row.eoff + rel + length]
        else:
            data = self._read_extent(
                row.disk_bytenr, row.disk_len, row.egen, row.eoff + rel, length
            )
        return data + bytes(length - len(data)) if len(data) < length else data

    def _read_extent(self, bytenr: int, disk_len: int, egen: int, start: int, length: int) -> bytes:
        """`length` bytes at `start` within a data extent, with patched sectors replaced."""
        patch = self._patch((bytenr, disk_len, egen))
        try:
            data = self.read_logical(bytenr + start, length)
        except DataError:
            if patch is None:
                raise
            data = bytes(length)  # location unknown, but copies of some sectors are known
        if patch is None:
            return data
        phys, kind = patch
        ss = self.sectorsize
        first, last = start // ss, min(len(kind), -(-(start + length) // ss))
        sel = np.flatnonzero(kind[first:last]) + first
        if not len(sel):
            return data
        out = bytearray(data.ljust(length, b"\0"))
        for s in sel:
            s = int(s)
            lo, hi = max(start, s * ss), min(start + length, (s + 1) * ss)
            zero = kind[s] == PATCH_ZERO
            sector = bytes(ss) if zero else self.dev.pread(ss, int(phys[s]))
            out[lo - start : hi - start] = sector[lo - s * ss : hi - s * ss]
        return bytes(out)

    # ------------------------------------------------------------------ sector patches

    def _patch(self, key: tuple[int, int, int]) -> tuple[np.ndarray, np.ndarray] | None:
        """(physical offset per sector, patch kind per sector) of an extent, or None."""
        if self._patch_rows is None:
            self._patch_rows = {
                (b, n, g): blob
                for b, n, g, blob in self.con.execute(
                    "SELECT disk_bytenr, disk_len, egen, patch FROM sector_patch"
                )
            }
        blob = self._patch_rows.get(key)
        if blob is None:
            return None
        hit = self._patch_arrays.get(key)
        if hit is None:
            raw = zlib.decompress(blob)
            nsect = len(raw) // 9
            hit = (
                np.frombuffer(raw[: nsect * 8], dtype=np.int64),
                np.frombuffer(raw[nsect * 8 :], dtype=np.uint8),
            )
            self._patch_arrays.put(key, hit)
        return hit

    def reset_patches(self) -> None:
        """Reload patches (after `match`), and extent results they apply to."""
        self._patch_rows = None
        self._patch_arrays = _LRU(256)
        self._ext_status = None
        self._decoded = _LRU(64)

    def _patched(self, key: tuple[int, int, int], c: ExtentCheck) -> ExtentCheck:
        """`c` with patched bad sectors counted as good."""
        patch = self._patch(key)
        if patch is None:
            return c
        _phys, kind = patch
        nsect = len(kind)
        if c.sectors is not None:
            states = np.frombuffer(zlib.decompress(c.sectors), dtype=np.uint8).copy()
        elif not c.mapped:
            states = np.full(nsect, SECTOR_BAD, dtype=np.uint8)
        elif c.good + c.bad + c.nocsum != nsect:  # sampled (quick) result
            return c
        else:
            st = SECTOR_OK if c.good else SECTOR_BAD if c.bad else SECTOR_NOCSUM
            states = np.full(nsect, st, dtype=np.uint8)
        fix = (states == SECTOR_BAD) & (kind != PATCH_NONE)
        if not fix.any():
            return c
        states[fix] = SECTOR_OK
        n = np.bincount(states, minlength=3)
        good, bad, nocsum = int(n[SECTOR_OK]), int(n[SECTOR_BAD]), int(n[SECTOR_NOCSUM])
        res = ExtentCheck(good, bad, nocsum, min(c.zero, bad), 1, None, int(fix.sum()))
        if not res.uniform:
            res = res._replace(sectors=zlib.compress(states.tobytes(), 1))
        return res

    def raw_extent_status(self, key: tuple[int, int, int]) -> ExtentCheck:
        """The stored on-disk check of an extent, without patches."""
        row = self.con.execute(
            "SELECT good, bad, nocsum, zero, mapped, sectors FROM extent_status "
            "WHERE disk_bytenr=? AND disk_len=? AND egen=?",
            key,
        ).fetchone()
        return ExtentCheck(*row) if row else ExtentCheck(0, 0, 0, 0, 0)

    # ------------------------------------------------------------------ reading files

    def read(
        self, tree: int, ino: int, offset: int, size: int, errors: list[str] | None = None
    ) -> bytes:
        lay = self.layout(tree, ino)
        end = min(offset + size, lay.size)
        if offset >= end:
            return b""
        out = bytearray(end - offset)
        for a, b, row in lay.segments:
            lo, hi = max(a, offset), min(b, end)
            if lo >= hi:
                continue
            try:
                out[lo - offset : hi - offset] = self._segment_bytes(row, lo - row.foff, hi - lo)
            except DataError as err:
                if errors is not None:
                    errors.append(f"[{lo}:{hi}) {err}")
        return bytes(out)

    def iter_content(
        self, tree: int, ino: int, block: int = 8 << 20, errors: list[str] | None = None
    ) -> Iterator[bytes]:
        size = self.layout(tree, ino).size
        for off in range(0, size, block):
            yield self.read(tree, ino, off, block, errors)

    def readlink(self, tree: int, ino: int) -> str:
        return os.fsdecode(self.read(tree, ino, 0, 4096))

    # ------------------------------------------------------------------ verification

    def _csum_leaf(self, phys: int, gen: int) -> list[tuple[int, bytes]] | None:
        key = (phys, gen)
        if key not in self._csum_leaves:
            block = self.read_node(phys, gen)
            items = None
            if block is not None:
                items = [
                    (it.offset, it.data)
                    for it in od.leaf_items(block, self.nodesize)
                    if it.type == od.EXTENT_CSUM
                ]
            self._csum_leaves.put(key, items)
        return self._csum_leaves[key]

    def _data_csums(self, logical: int, nsect: int, egen: int) -> list[bytes | None]:
        """Expected per-sector checksums, from the oldest csum leaf written at/after egen."""
        ss, csz = self.sectorsize, self._csum_size
        out: list[bytes | None] = [None] * nsect
        hi = logical + nsect * ss - 1
        lo = logical - (self.nodesize // csz) * ss
        if self._csum_index is None:
            self._csum_index = _CsumIndex(self.con, self.fsid)
        leaves = self._csum_index.find(lo, hi, egen)
        missing = nsect
        for phys, gen in leaves:
            items = self._csum_leaf(phys, gen)
            for offset, data in items or ():
                count = len(data) // csz
                first = max(0, (offset - logical) // ss)
                last = min(nsect, (offset + count * ss - logical) // ss)
                for i in range(first, last):
                    j = (logical + i * ss - offset) // ss
                    if out[i] is None and 0 <= j < count:
                        out[i] = data[j * csz : (j + 1) * csz]
                        missing -= 1
            if missing == 0:
                break
        return out

    def _load_extent_status(self) -> dict[tuple[int, int, int], ExtentCheck]:
        """Stored extent checks, with sector patches applied."""
        if self._ext_status is None:
            self._ext_status = {
                (b, n, g): self._patched((b, n, g), ExtentCheck(*rest))
                for b, n, g, *rest in self.con.execute(
                    "SELECT disk_bytenr, disk_len, egen, good, bad, nocsum, zero, mapped, "
                    "sectors FROM extent_status"
                )
            }
        return self._ext_status

    def check_extent(
        self,
        disk_bytenr: int,
        disk_len: int,
        egen: int,
        quick: bool = False,
        refresh: bool = False,
        comp: int = 0,
    ) -> ExtentCheck:
        """Compare an extent's sectors with the recorded data checksums (patches applied).

        Results are cached; `refresh` re-reads the device. See `check_extent_raw`.
        """
        cache = self._load_extent_status()
        key = (disk_bytenr, disk_len, egen)
        if key in cache and not refresh:
            return cache[key]
        res = self._patched(key, self.check_extent_raw(disk_bytenr, disk_len, egen, quick, comp))
        if not quick:
            cache[key] = res
        return res

    def check_extent_raw(
        self, disk_bytenr: int, disk_len: int, egen: int, quick: bool = False, comp: int = 0
    ) -> ExtentCheck:
        """Compare an extent's on-disk sectors with the recorded data checksums.

        Sectors without a checksum count as bad when they read as all zeros, or when the
        extent is compressed (`comp`) and does not decompress - both mean nothing is there.
        """
        ss = self.sectorsize
        nsect = max(1, -(-disk_len // ss))
        try:
            phys = self.map_logical(disk_bytenr)
        except DataError:
            phys = []
        if not phys:
            res = ExtentCheck(0, 0, 0, 0, 0)
        else:
            sums = self._data_csums(disk_bytenr, nsect, egen)
            idx = sorted({0, nsect // 2, nsect - 1}) if quick else range(nsect)
            data = None if quick else self.dev.pread(nsect * ss, phys[0])
            good = bad = nocsum = zero = 0
            states = bytearray(nsect)
            for i in idx:
                want = sums[i]
                if data is None:
                    if want is None:
                        nocsum += 1
                        states[i] = SECTOR_NOCSUM
                        continue
                    sector = self.dev.pread(ss, phys[0] + i * ss)
                else:
                    sector = data[i * ss : (i + 1) * ss]
                if want is None:
                    if sector.count(0) == len(sector):  # no checksum and nothing there
                        bad += 1
                        zero += 1
                        states[i] = SECTOR_BAD
                    else:
                        nocsum += 1
                        states[i] = SECTOR_NOCSUM
                    continue
                if self._csum(sector) == want:
                    good += 1
                else:
                    bad += 1
                    states[i] = SECTOR_BAD
                    zero += sector == bytes(len(sector))
            if comp and nocsum and data is not None:
                try:
                    self._decompress(data[:disk_len], comp)
                except DataError:  # unverifiable and undecodable: garbage
                    for i, st in enumerate(states):
                        if st == SECTOR_NOCSUM:
                            states[i] = SECTOR_BAD
                    bad, nocsum = bad + nocsum, 0
            res = ExtentCheck(good, bad, nocsum, zero, 1)
            if not quick and not res.uniform:
                res = res._replace(sectors=zlib.compress(bytes(states), 1))
        return res

    def verify_extent(self, row: ExtentRow) -> str:
        """Human status of one extent: ok | bad | zeroed | mixed | partial | nocsum | ..."""
        if row.etype == od.FILE_EXTENT_INLINE:
            return "inline"
        if not row.is_data:
            return "hole" if row.disk_bytenr == 0 else "prealloc"
        c = self.check_extent(row.disk_bytenr, row.disk_len, row.egen, comp=row.comp)
        if not c.mapped:
            return "unmapped"
        if c.bad:
            status = "mixed" if c.good else "zeroed" if c.zero == c.bad else "bad"
        elif c.nocsum:
            status = "partial" if c.good else "nocsum"
        else:
            status = "ok"
        return f"{status}, {c.copied} sectors from copies" if c.copied else status

    def file_check(self, tree: int, ino: int) -> tuple[str, str]:
        """(category, detail) for a regular file in this view."""
        key = (tree, ino, self.at_gen)
        hit = self._cats.get(key)
        if hit is not None and hit[1] is not None:
            return hit
        lay = self.layout(tree, ino)
        good = bad = nocsum = unmapped = copied = 0
        ss = self.sectorsize
        seen: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        comps: dict[tuple[int, int, int], int] = {}
        for a, b, row in lay.segments:
            if not row.is_data:
                continue
            k = (row.disk_bytenr, row.disk_len, row.egen)
            nsect = max(1, -(-row.disk_len // ss))
            if row.comp:  # compressed: the whole extent is needed to decode any part
                rng = (0, nsect)
            else:  # only the sectors this file range actually uses
                lo = row.eoff + (a - row.foff)
                rng = (lo // ss, -(-(lo + b - a) // ss))
            seen.setdefault(k, []).append(rng)
            comps[k] = row.comp
        for k, ranges in seen.items():
            c = self.check_extent(*k, comp=comps[k])
            if not c.mapped:
                unmapped += 1
                continue
            g, bd, nc = c.counts(ranges, max(1, -(-k[1] // ss)))
            good, bad, nocsum, copied = good + g, bad + bd, nocsum + nc, copied + c.copied
        if not lay.segments and lay.size > 0:
            res = (CAT_LOST, "no data extents recovered")
        elif not seen:
            inline = any(r.etype == od.FILE_EXTENT_INLINE for _a, _b, r in lay.segments)
            if inline or lay.size == 0:
                res = (CAT_INTACT, "inline" if inline else "empty")
            else:
                res = (CAT_LOST, "no data, only holes")
        else:
            detail = f"sectors ok {good}, bad {bad}, unverifiable {nocsum}"
            if unmapped:
                detail += f", {unmapped} unmapped extents"
            if copied:
                detail += f" ({copied} recovered from copies)"
            if bad == 0 and unmapped == 0:
                res = (CAT_UNVERIFIED if nocsum else CAT_INTACT, detail)
                if nocsum and self._implausible(tree, ino):
                    # no checksum, and the start does not fit the file type: garbage there;
                    # later extents may still be fine
                    cat = CAT_LOST if len(seen) <= 1 else CAT_DAMAGED
                    res = (cat, detail + "; start does not match the file type")
            elif good == 0 and nocsum == 0:
                res = (CAT_LOST, detail)
            else:
                res = (CAT_DAMAGED, detail)
        total = good + bad + nocsum
        frac = bad / total if total else (1.0 if res[0] == CAT_LOST else 0.0)
        self._cats[key] = res
        self._file_stats[key] = (lay.size, frac)
        return res

    def problem_ranges(self, tree: int, ino: int) -> list[tuple[int, int, str]]:
        """[(start, end, "bad" | "unverified")] byte ranges of a file in this view, merged.

        A compressed extent with any bad sector is bad as a whole (it cannot be decoded).
        """
        ss = self.sectorsize
        out: list[tuple[int, int, str]] = []

        def add(lo: int, hi: int, kind: str) -> None:
            if hi <= lo:
                return
            if out and out[-1][2] == kind and out[-1][1] >= lo:
                out[-1] = (out[-1][0], max(out[-1][1], hi), kind)
            else:
                out.append((lo, hi, kind))

        for a, b, row in self.layout(tree, ino).segments:
            if not row.is_data:
                continue
            c = self.check_extent(row.disk_bytenr, row.disk_len, row.egen, comp=row.comp)
            if not c.mapped:
                add(a, b, "bad")
                continue
            if row.comp or c.uniform or c.sectors is None:
                if c.bad:
                    add(a, b, "bad")
                elif c.nocsum:
                    add(a, b, "unverified")
                continue
            states = np.frombuffer(zlib.decompress(c.sectors), dtype=np.uint8)
            first = (row.eoff + a - row.foff) // ss
            last = -(-(row.eoff + b - row.foff) // ss)
            for s in range(first, min(last, len(states))):
                st = states[s]
                if st == SECTOR_OK:
                    continue
                lo = max(a, row.foff - row.eoff + s * ss)
                hi = min(b, row.foff - row.eoff + (s + 1) * ss)
                add(lo, hi, "bad" if st == SECTOR_BAD else "unverified")
        return out

    def compare_content(self, tree: int, ino: int, content: bytes) -> tuple[int, int]:
        """(matching, compared) sectors of `content` against this file's expected checksums.

        Only uncompressed data extents with recorded checksums can be compared (compressed
        extents are checksummed after compression). The last sector of a file is compared
        with its zero padding, as btrfs writes it.
        """
        ss = self.sectorsize
        lay = self.layout(tree, ino)
        match = compared = 0
        for a, b, row in lay.segments:
            if not row.is_data or row.comp:
                continue
            nsect = max(1, -(-row.disk_len // ss))
            sums = self._data_csums(row.disk_bytenr, nsect, row.egen)
            base = row.foff - row.eoff  # file offset of the extent's first sector
            for s in range(max(0, (a - base) // ss), min(nsect, -(-(b - base) // ss))):
                lo = base + s * ss
                if sums[s] is None or lo < a:
                    continue
                if lo + ss <= b:
                    data = content[lo : lo + ss]
                elif b == lay.size:  # file tail, zero padded on disk
                    data = content[lo:b]
                else:
                    continue
                if len(data) < ss:
                    data = data + bytes(ss - len(data))
                compared += 1
                match += self._csum(data) == sums[s]
        return match, compared

    def _has_data(self, tree: int, ino: int, lo: int, hi: int) -> bool:
        """True if [lo, hi) is fully covered by data (not holes/prealloc) in this view."""
        at = lo
        for a, b, row in self.layout(tree, ino).segments:
            if b <= at or a >= hi:
                continue
            if a > at or not (row.is_data or row.etype == od.FILE_EXTENT_INLINE):
                return False
            at = b
            if at >= hi:
                return True
        return at >= hi

    def _name_of(self, tree: int, ino: int) -> str:
        row = self.con.execute(
            "SELECT name FROM dirents WHERE tree=? AND child=? AND child_kind=1 "
            "ORDER BY gen_max DESC LIMIT 1",
            (tree, ino),
        ).fetchone()
        return fsname(row[0]) if row else ""

    def _implausible(self, tree: int, ino: int) -> bool:
        """True if the file's first bytes contradict the type its (latest) name implies."""
        head = self.read(tree, ino, 0, SNIFF_BYTES)
        return plausible(self._name_of(tree, ino), head) is False

    def has_content(self, entry: Entry) -> bool:
        """False for a non-empty file that would read back as nothing but zeros.

        Cheap (no data is read): true when some part of the file is inline or a mapped
        data extent. Directories, symlinks and empty files always count as having content.
        """
        if entry.is_dir or entry.ftype == od.FT_SYMLINK:
            return True
        lay = self.layout(entry.node.tree, entry.node.ino)
        if lay.size == 0:
            return True
        for _a, _b, row in lay.segments:
            if row.etype == od.FILE_EXTENT_INLINE:
                return True
            if row.is_data:
                if self._patch((row.disk_bytenr, row.disk_len, row.egen)) is not None:
                    return True
                try:
                    if self.map_logical(row.disk_bytenr):
                        return True
                except DataError:
                    pass
        return False

    def version_gens(self, tree: int, ino: int) -> list[int]:
        """Generations at which the file's content changed (newest first, before this view)."""
        clause, args = ("", ()) if self.at_gen is None else (" AND gen_min < ?", (self.at_gen,))
        return [
            g
            for (g,) in self.con.execute(
                "SELECT DISTINCT gen_min FROM extents WHERE tree=? AND ino=?"
                + clause
                + " ORDER BY gen_min DESC LIMIT ?",
                (tree, ino, *args, MAX_VERSIONS),
            )
        ]

    def _score(self, tree: int, ino: int) -> tuple[int, float]:
        """Lower is better: (category rank, fraction of bad sectors)."""
        cat = self.file_check(tree, ino)[0]
        return CAT_RANK[cat], self._file_stats.get((tree, ino, self.at_gen), (0, 0.0))[1]

    def best_version(self, tree: int, ino: int) -> tuple[int | None, str]:
        """(generation to read, category) of the newest version with the best content.

        None = this view's own version. An older version is chosen only when it is not empty
        and strictly better: a better category (intact > unverified > damaged > lost) or, for
        damaged files, fewer bad sectors. A file that is empty now but had content before
        (typically truncated when disaster struck) falls back to that older content.
        """
        self._load_masks()
        key = (tree, ino, self.at_gen)
        if key in self._best:
            return self._best[key]
        if self._best_stored and key in self._cats:  # stored: no fallback for this file
            return None, self._cats[key][0]
        cat = self.file_check(tree, ino)[0]
        best: tuple[int | None, str] = (None, cat)
        empty = cat in GOOD_CATS and self.layout(tree, ino).size == 0
        if (cat not in GOOD_CATS or empty) and not self._best_stored:
            score = (CAT_RANK[CAT_LOST], 0.0) if empty else self._score(tree, ino)
            for g in self.version_gens(tree, ino):
                old = self.view_at(g)
                if old.layout(tree, ino).size == 0:
                    continue
                s = old._score(tree, ino)
                if s < score:
                    score, best = s, (g, old.file_check(tree, ino)[0])
                    if best[1] in GOOD_CATS:
                        break
        self._best[key] = best
        return best

    def best_entry_view(self, entry: Entry) -> RescueFS:
        """The view to stat/read `entry` from in the best/ folder."""
        if entry.is_dir or entry.ftype == od.FT_SYMLINK:
            return self
        return self.view_at(self.best_version(entry.node.tree, entry.node.ino)[0])

    def verify_file(self, tree: int, ino: int) -> str:
        return self.file_check(tree, ino)[0]

    def category(self, entry: Entry) -> str | None:
        """Category of a file entry (None for directories)."""
        if entry.is_dir:
            return None
        if entry.ftype == od.FT_SYMLINK:
            self._cats.setdefault((entry.node.tree, entry.node.ino, self.at_gen), (CAT_INTACT, ""))
            return CAT_INTACT
        hit = self._cats.get((entry.node.tree, entry.node.ino, self.at_gen))
        return hit[0] if hit else self.file_check(entry.node.tree, entry.node.ino)[0]

    # ------------------------------------------------------------------ category masks

    @staticmethod
    def _mask_key(node: Node) -> tuple[int, int]:
        return (node.tree, -1 if node.kind == KIND_ORPHANS else node.ino)

    def masks_valid(self) -> bool:
        """True when a stored classification matches this view's exclude list."""
        ex = frozenset(self.exclude)
        if self._masks_valid_for != ex:
            self._masks_valid_for = ex
            self._masks_valid = get_meta(self.con, "classify_exclude") == self._classify_key()
        return self._masks_valid

    def _classify_key(self) -> str:
        return classify_key(self.exclude, get_meta(self.con, "patch_serial", ""))

    def classified(self) -> bool:
        return get_meta(self.con, "classify_exclude") is not None

    def _load_masks(self) -> None:
        if self._masks is not None:
            return
        self._masks = {}
        if self.at_gen is None and self.masks_valid():
            self._masks = {(t, i): m for t, i, m in self.con.execute("SELECT * FROM node_mask")}
            for t, i, cat, _size in self.con.execute("SELECT * FROM file_cat"):
                self._cats.setdefault((t, i, None), (cat, None))
            for t, i, g, cat in self.con.execute("SELECT * FROM best_version"):
                self._best[(t, i, None)] = (g, cat)
            self._best_stored = True

    def entry_mask(self, entry: Entry) -> int:
        """Bitmask of categories present at or below this entry (visible names only)."""
        if not entry.is_dir:
            cat = self.category(entry)
            if not cat:
                return 0
            bits = CAT_BITS[cat]
            if entry.ftype == od.FT_SYMLINK:
                return bits | BEST_BIT
            if self.best_version(entry.node.tree, entry.node.ino)[1] != CAT_LOST:
                bits |= BEST_BIT
            return bits
        return self.dir_mask(entry.node)

    def dir_mask(self, node: Node) -> int:
        self._load_masks()
        key = self._mask_key(node)
        if key in self._masks:
            return self._masks[key]
        # iterative post-order walk; provisional 0 guards against cycles
        stack: list[tuple[Node, list[Entry] | None]] = [(node, None)]
        while stack:
            cur, kids = stack[-1]
            ck = self._mask_key(cur)
            if kids is None:
                if ck in self._masks:
                    stack.pop()
                    continue
                self._masks[ck] = 0
                kids = [e for e in self.children(cur).values() if not e.stale]
                stack[-1] = (cur, kids)
                for k in kids:
                    if k.is_dir and self._mask_key(k.node) not in self._masks:
                        stack.append((k.node, None))
                continue
            m = 0
            for k in kids:
                m |= self.dir_mask_cached(k) if k.is_dir else self.entry_mask(k)
            self._masks[ck] = m
            stack.pop()
        return self._masks[key]

    def dir_mask_cached(self, entry: Entry) -> int:
        return self._masks.get(self._mask_key(entry.node), 0)

    def save_classification(self) -> None:
        """Persist masks and file categories of this (latest) view."""
        con = self.con
        con.execute("DELETE FROM node_mask")
        con.execute("DELETE FROM file_cat")
        con.execute("DELETE FROM best_version")
        con.executemany(
            "INSERT OR REPLACE INTO best_version VALUES (?,?,?,?)",
            [(t, i, g, c) for (t, i, v), (g, c) in self._best.items() if v is None and g],
        )
        con.executemany(
            "INSERT OR REPLACE INTO node_mask VALUES (?,?,?)",
            [(t, i, m) for (t, i), m in (self._masks or {}).items()],
        )
        rows = []
        for (t, i, g), (cat, _d) in self._cats.items():
            if g is None:
                size = self._file_stats.get((t, i, None), (None,))[0]
                rows.append((t, i, cat, self.layout(t, i).size if size is None else size))
        con.executemany("INSERT OR REPLACE INTO file_cat VALUES (?,?,?,?)", rows)
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('classify_exclude', ?)",
            (self._classify_key(),),
        )
        con.commit()
        self._masks_valid_for = None

    def best_fallbacks(self) -> int:
        """Files for which best/ serves an older version (persisted classification)."""
        return self.con.execute("SELECT count(*) FROM best_version").fetchone()[0]

    def category_totals(self) -> dict[str, tuple[int, int]]:
        """{category: (files, bytes)} from the persisted classification."""
        out = dict.fromkeys(CATEGORIES, (0, 0))
        for cat, n, b in self.con.execute(
            "SELECT cat, count(*), coalesce(sum(size), 0) FROM file_cat GROUP BY cat"
        ):
            out[cat] = (n, b)
        return out


def classify_key(exclude, patch_serial: str = "") -> str:
    """Identifies stored categories: valid only for these rules, this exclude list and these
    sector patches."""
    return json.dumps({"v": MASK_VERSION, "exclude": sorted(exclude), "patches": patch_serial})


def paint(rows: Iterator[ExtentRow] | list[ExtentRow]) -> list[tuple[int, int, ExtentRow]]:
    """Lay extents over each other in order (later rows cover earlier ones).

    Returns disjoint [start, end) segments sorted by start. O(n log n) plus list moves, so
    files with tens of thousands of extent records stay fast.
    """
    starts: list[int] = []
    segs: list[tuple[int, int, ExtentRow]] = []
    for row in rows:
        s, e = row.foff, row.foff + row.length
        if e <= s:
            continue
        lo = bisect.bisect_right(starts, s) - 1
        if lo < 0 or segs[lo][1] <= s:
            lo += 1  # first segment ending after s
        hi = bisect.bisect_left(starts, e)  # segments from hi on start at/after e
        repl = []
        if lo < hi and segs[lo][0] < s:
            a, _b, r = segs[lo]
            repl.append((a, s, r))
        repl.append((s, e, row))
        if lo < hi and segs[hi - 1][1] > e:
            _a, b, r = segs[hi - 1]
            repl.append((e, b, r))
        segs[lo:hi] = repl
        starts[lo:hi] = [x[0] for x in repl]
    return segs


class _CsumIndex:
    """In-memory index of checksum-tree leaves by covered logical range.

    Leaves are sorted by first key; most span little, so a binary search bounded by the
    widest "narrow" span finds candidates. The few wide leaves are checked vectorised.
    """

    NARROW = 64 << 20

    def __init__(self, con: sqlite3.Connection, fsid: bytes | None):
        rows = con.execute(
            "SELECT first_off, last_off, gen, phys FROM nodes "
            "WHERE fsid=? AND owner=? AND level=0 AND csum_ok=1 AND first_off IS NOT NULL "
            "AND last_off IS NOT NULL ORDER BY first_off",
            (fsid, od.CSUM_TREE),
        ).fetchall()
        a = np.array(rows, dtype=np.int64).reshape(-1, 4)
        wide = (a[:, 1] - a[:, 0]) > self.NARROW
        self.narrow, self.wide = a[~wide], a[wide]
        self.first = self.narrow[:, 0]

    def find(self, lo: int, hi: int, gen: int, limit: int = 64) -> list[tuple[int, int]]:
        """[(phys, gen)] of leaves overlapping [lo, hi] written at/after `gen`, oldest first."""
        i = np.searchsorted(self.first, lo - self.NARROW, "left")
        j = np.searchsorted(self.first, hi, "right")
        cand = self.narrow[i:j]
        cand = cand[(cand[:, 1] >= lo) & (cand[:, 2] >= gen)]
        w = self.wide
        w = w[(w[:, 0] <= hi) & (w[:, 1] >= lo) & (w[:, 2] >= gen)]
        both = np.concatenate([cand, w]) if len(w) else cand
        both = both[np.lexsort((both[:, 3], both[:, 2]))][:limit]
        return [(int(p), int(g)) for g, p in both[:, 2:4]]


def _ftype_from_mode(mode: int) -> int:
    fmt = mode & od.S_IFMT
    if fmt == od.S_IFDIR:
        return od.FT_DIR
    if fmt == od.S_IFLNK:
        return od.FT_SYMLINK
    return od.FT_REG
