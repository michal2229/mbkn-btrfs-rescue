"""Read-only FUSE mount of the recovered namespace (optional dependency: mfusepy).

Layout (default):

    <mnt>/README.txt                     what the folders mean, with counts
    <mnt>/best/<subvol>@<id>/...         newest version with good content (older if newest broke)
    <mnt>/intact/<subvol>@<id>/...       files whose content verifies
    <mnt>/unverified/<subvol>@<id>/...   no checksum on record - probably fine
    <mnt>/damaged/<subvol>@<id>/...      partially readable
    <mnt>/lost/<subvol>@<id>/...         content gone; names and metadata only
    <mnt>/all/<subvol>@<id>/...          everything readable, newest version of each file
    <mnt>/history/gen-0001100/...        everything readable as seen up to generation 1100

Every category folder keeps the original directory structure, showing only directories that
contain files of that category. Categories need `classify`; without it only all/ and history/
are populated. all/ and history/ hide files with no recoverable data (they would read as
zeros, or once classified: lost files); they stay listed in lost/. With a fixed `at_gen` (mount --at-gen N) or --flat, one view is mounted at <mnt>/.
"""

from __future__ import annotations

import csv
import errno
import io
import json
import os
import sqlite3
import stat as stat_mod
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from .db import get_meta
from .model import (
    BEST_BIT,
    CAT_BITS,
    CAT_HELP,
    CAT_LOST,
    CATEGORIES,
    ORPHANS_NAME,
    PATCH_BIT,
    ROOT,
    Entry,
    RescueFS,
)

try:
    import mfusepy as fuse
except ImportError:  # pragma: no cover - optional extra
    fuse = None

BEST, PATCHED, ALL, HISTORY, CURRENT = "best", "patched", "all", "history", "current"
GEN_PREFIX, README, PATCHED_TSV = "gen-", "README.txt", "PATCHED.tsv"


def _available() -> None:
    if fuse is None:
        raise SystemExit("FUSE support needs the optional extra: uv sync --extra fuse")


def gen_dirname(gen: int) -> str:
    return f"{GEN_PREFIX}{gen:07d}"


def _enoent():
    return fuse.FuseOSError(errno.ENOENT)


def readme_text(fs: RescueFS | None, con: sqlite3.Connection) -> str:
    state = get_meta(con, "analyze_state")
    status = get_meta(con, "analyze_status")
    lines = ["mbkn-btrfs-rescue - recovered filesystem (read-only)", ""]
    if state != "complete" and status:
        lines += [
            f"ANALYSIS IN PROGRESS ({state}): {status}",
            "Folders fill in as the scan proceeds; this mount refreshes automatically.",
            "Category folders (intact/ ...) are populated when classification finishes.",
            "",
        ]
    warnings = json.loads(get_meta(con, "compat_warnings") or "[]")
    if warnings:
        lines += ["WARNING - this filesystem uses features outside what is supported:"]
        lines += [f"  * {w}" for w in warnings] + [""]
    if fs is None:
        return "\n".join([*lines, "No filesystem index yet - waiting for the scan.", ""])
    lines += [
        "Every folder keeps the original paths: <subvolume>@<tree id>/original/path.",
        "Renamed/moved files appear under their newest name; files whose directory could not",
        "be recovered are in <subvolume>@<id>/.orphans/<inode>_<last known name>.",
        "",
    ]
    classified = fs.masks_valid()
    totals = fs.category_totals() if classified else {}
    older = fs.best_fallbacks() if classified else 0
    npatched = patched_count(con) if classified else 0
    pending = "  (run classify)"

    def row(name: str, count: str, text: str) -> str:
        return f"  {name + '/':12} {count:29}   {text}"

    lines.append(
        row(
            BEST,
            f"{older:>8} older versions used" if classified else pending,
            "START HERE: the most complete version of every file",
        )
    )
    lines.append(
        row(
            PATCHED,
            f"{npatched:>8} files" if classified else pending,
            f"the files of best/ that are reconstructed (see {PATCHED_TSV})",
        )
    )
    for cat in CATEGORIES:
        n, size = totals.get(cat, (0, 0))
        count = f"{n:>8} files {size / 2**20:>10.1f} MiB" if classified else pending
        lines.append(row(cat, count, CAT_HELP[cat]))
    lines.append(row(ALL, "", "everything readable, newest known version of each file"))
    lines.append(row(HISTORY, "", "everything readable up to a generation (older versions)"))
    if get_meta(con, "current_built") is not None:
        missing = int(get_meta(con, "current_missing", "0"))
        lines.append(
            row(CURRENT, "", "the disk as a normal mount shows it now")
            + (f" ({missing} tree blocks unreadable: names missing)" if missing else "")
        )
    lines += [
        "",
        "best/ serves, for each file, the first of these that exists:",
        "  1. verified content: the newest version that is intact (or unverified but",
        "     plausible), falling back to older versions; bad sectors are already replaced by",
        "     identical copies found elsewhere on the disk",
        "  2. a git blob identical to the lost file (index / stash / HEAD)",
        "  3. the best damaged version with all bad ranges filled from older versions",
        "  4. a git blob of the same size, then 5. an older git blob of another size",
        "  6. the best damaged version, partly filled, or as it is (garbage in the bad parts)",
        f"Cases 2-6 are also listed in {PATCHED}/ and in {PATCHED_TSV} (source of each file):",
        "check those before relying on them. Files show the timestamps of the version served.",
        "lost/ lists what existed, but its content could not be recovered.",
        *_copies_line(con),
        "all/ and history/ leave out files without recoverable data (zeros or lost).",
        f"Hidden names: {', '.join(sorted(fs.exclude)) or '(none)'}",
        "",
    ]
    return "\n".join(lines)


def patched_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT count(*) FROM file_patch").fetchone()[0]


def patched_tsv(fs: RescueFS) -> str:
    """One row per reconstructed file: path (as in best/), source, complete, detail."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(["path", "source", "complete", "detail"])
    rows = []
    for t, i, src, complete, detail in fs.con.execute(
        "SELECT tree, ino, source, complete, detail FROM file_patch"
    ):
        rows.append([_path_in_mount(fs, t, i), src, "yes" if complete else "no", detail])
    w.writerows(sorted(rows))
    return buf.getvalue()


def _path_in_mount(fs: RescueFS, tree: int, ino: int) -> str:
    path = fs.path_of(tree, ino)
    if path is not None:
        return path
    for e in fs.orphans(tree):  # directly in .orphans/ (named <inode>_<last name>)
        if e.node.ino == ino:
            return f"/{fs.tree_label(tree)}/{ORPHANS_NAME}/{e.name}"
    return f"/{fs.tree_label(tree)}/{ORPHANS_NAME}/.../{ino}"


def _copies_line(con: sqlite3.Connection) -> list[str]:
    row = con.execute("SELECT count(*), sum(copied), sum(zeros) FROM sector_patch").fetchone()
    if not row or not row[0]:
        return []
    return [
        f"Bad sectors replaced everywhere: {row[1] or 0} from identical copies found on the "
        f"disk, {row[2] or 0} known to be zeros.",
    ]


class RescueOps(fuse.Operations if fuse else object):  # type: ignore[misc]
    use_ns = False

    def __init__(
        self,
        fs: RescueFS | None,
        layered: bool = True,
        *,
        con: sqlite3.Connection | None = None,
        reload: Callable[[], RescueFS | None] | None = None,
        refresh: float = 30.0,
        show_unreadable: bool = False,
    ):
        self.fs = fs
        self.layered = layered
        self.show_unreadable = show_unreadable
        self._con = con or (fs.con if fs else None)
        self._reload = reload
        self._refresh = refresh
        self._last_check = time.monotonic()
        self._data_version = self._db_version()
        self._mutex = threading.Lock()
        self._views: OrderedDict[int, RescueFS] = OrderedDict()
        self._gens: list[int] | None = None
        self._readme: bytes | None = None
        self._patched_tsv: bytes | None = None
        self._current: RescueFS | None = None
        self.mounted_at = time.time()

    # ------------------------------------------------------------------ live refresh

    def _db_version(self) -> int | None:
        if self._con is None:
            return None
        return self._con.execute("PRAGMA data_version").fetchone()[0]

    def _maybe_refresh(self) -> None:
        """Reload the index view when another connection committed since the last check."""
        if self._reload is None or time.monotonic() - self._last_check < self._refresh:
            return
        self._last_check = time.monotonic()
        version = self._db_version()
        if version == self._data_version:
            return
        self._data_version = version
        self.fs = self._reload()
        self._views.clear()
        self._gens = None
        self._readme = None
        self._patched_tsv = None
        self._current = None

    # ------------------------------------------------------------------ routing

    def _gen_list(self) -> list[int]:
        assert self.fs is not None
        if self._gens is None:
            self._gens = self.fs.generations()
        return self._gens

    def _view(self, gen: int) -> RescueFS:
        assert self.fs is not None
        if gen not in self._views:
            self._views[gen] = self.fs.at(gen)
            while len(self._views) > 16:
                self._views.popitem(last=False)
        self._views.move_to_end(gen)
        return self._views[gen]

    def _readme_bytes(self) -> bytes:
        if self._readme is None:
            self._readme = readme_text(self.fs, self._con).encode() if self._con else b""
        return self._readme

    def _patched_bytes(self) -> bytes:
        if self._patched_tsv is None:
            self._patched_tsv = patched_tsv(self.fs).encode() if self.fs else b""
        return self._patched_tsv

    def _virtual_file(self, name: str) -> bytes:
        return self._readme_bytes() if name == README else self._patched_bytes()

    def _route(self, path: str):
        """-> ("virtual", [names]) | ("readme", None) | ("fs", (view, subpath, category|None))"""
        self._maybe_refresh()
        parts = [p for p in path.split("/") if p]
        if self.fs is None:
            if not parts:
                return "virtual", [README]
            if parts == [README]:
                return "readme", README
            raise _enoent()
        if not self.layered:
            return "fs", (self.fs, path, None)
        if not parts:
            names = [README, PATCHED_TSV, BEST, PATCHED, *CATEGORIES, ALL, HISTORY]
            return "virtual", [*names, CURRENT] if self._has_current() else names
        head, rest = parts[0], "/" + "/".join(parts[1:])
        if head in (README, PATCHED_TSV) and len(parts) == 1:
            return "readme", head
        if head == ALL:
            return "fs", (self.fs, rest, None)
        if head == CURRENT and self._has_current():
            if self._current is None:
                self._current = self.fs.current_view()
            return "fs", (self._current, rest, CURRENT)
        if head in CAT_BITS or head in (BEST, PATCHED):
            return "fs", (self.fs, rest, head)
        if head == HISTORY:
            if len(parts) == 1:
                return "virtual", [gen_dirname(g) for g in self._gen_list()]
            name = parts[1]
            if name.startswith(GEN_PREFIX) and name[len(GEN_PREFIX) :].isdigit():
                gen = int(name[len(GEN_PREFIX) :])
                if gen in set(self._gen_list()):
                    return "fs", (self._view(gen), "/" + "/".join(parts[2:]), None)
        raise _enoent()

    def _has_current(self) -> bool:
        return self._con is not None and get_meta(self._con, "current_built") is not None

    def _visible(self, fs: RescueFS, e: Entry, cat: str | None) -> bool:
        if cat == CURRENT:
            return True  # as mounted: every current name, readable or not
        if e.stale:
            return False
        if cat is not None:
            bit = BEST_BIT if cat == BEST else PATCH_BIT if cat == PATCHED else CAT_BITS[cat]
            return fs.masks_valid() and bool(fs.entry_mask(e) & bit)
        if self.show_unreadable:
            return True
        if fs.at_gen is None and fs.masks_valid():  # classified: hide lost files and dirs
            m = fs.entry_mask(e) & ~(BEST_BIT | PATCH_BIT)
            return m == 0 or bool(m & ~CAT_BITS[CAT_LOST])
        return fs.has_content(e)

    def _resolve(self, fs: RescueFS, sub: str, cat: str | None) -> tuple[Entry, ...]:
        try:
            chain = fs.resolve(sub)
        except FileNotFoundError:
            raise _enoent() from None
        if any(not self._visible(fs, e, cat) for e in chain):
            raise _enoent()
        return chain

    @staticmethod
    def _file_view(fs: RescueFS, e: Entry, cat: str | None):
        return fs.best_reader(e) if cat in (BEST, PATCHED) else fs

    def _dir_attr(self) -> dict:
        t = self.mounted_at
        return {
            "st_mode": stat_mod.S_IFDIR | 0o555,
            "st_nlink": 2,
            "st_mtime": t,
            "st_ctime": t,
            "st_atime": t,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
        }

    # ------------------------------------------------------------------ operations

    def getattr(self, path, fh=None):
        with self._mutex:
            kind, arg = self._route(path)
            if kind == "virtual":
                return self._dir_attr()
            if kind == "readme":
                return self._dir_attr() | {
                    "st_mode": stat_mod.S_IFREG | 0o444,
                    "st_nlink": 1,
                    "st_size": len(self._virtual_file(arg)),
                }
            fs, sub, cat = arg
            chain = self._resolve(fs, sub, cat)
            if not chain:
                return self._dir_attr()
            return self._file_view(fs, chain[-1], cat).stat(chain[-1])

    def readdir(self, path, fh):
        with self._mutex:
            kind, arg = self._route(path)
            if kind == "virtual":
                return [".", "..", *arg]
            if kind == "readme":
                raise fuse.FuseOSError(errno.ENOTDIR)
            fs, sub, cat = arg
            chain = self._resolve(fs, sub, cat)
            node = chain[-1].node if chain else ROOT
            names = [n for n, e in fs.children(node).items() if self._visible(fs, e, cat)]
        return [".", "..", *names]

    def readlink(self, path):
        with self._mutex:
            kind, arg = self._route(path)
            if kind != "fs":
                raise fuse.FuseOSError(errno.EINVAL)
            fs, sub, cat = arg
            chain = self._resolve(fs, sub, cat)
            if not chain:
                raise fuse.FuseOSError(errno.EINVAL)
            e = chain[-1]
            return self._file_view(fs, e, cat).readlink(e.node.tree, e.node.ino)

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise fuse.FuseOSError(errno.EROFS)
        return 0

    def read(self, path, size, offset, fh):
        with self._mutex:
            kind, arg = self._route(path)
            if kind == "readme":
                return self._virtual_file(arg)[offset : offset + size]
            if kind != "fs":
                raise fuse.FuseOSError(errno.EISDIR)
            fs, sub, cat = arg
            chain = self._resolve(fs, sub, cat)
            if not chain:
                raise fuse.FuseOSError(errno.EISDIR)
            e = chain[-1]
            return self._file_view(fs, e, cat).read(e.node.tree, e.node.ino, offset, size)

    def statfs(self, path):
        return {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": 0,
            "f_bfree": 0,
            "f_bavail": 0,
            "f_namemax": 255,
        }


def mount(
    fs: RescueFS | None,
    mountpoint: str,
    foreground: bool = True,
    layered: bool = True,
    *,
    con: sqlite3.Connection | None = None,
    reload: Callable[[], RescueFS | None] | None = None,
    refresh: float = 30.0,
    show_unreadable: bool = False,
) -> None:
    _available()
    ops = RescueOps(
        fs, layered, con=con, reload=reload, refresh=refresh, show_unreadable=show_unreadable
    )
    fuse.FUSE(
        ops,
        mountpoint,
        foreground=foreground,
        ro=True,
        nothreads=True,
        # read-only content: let the kernel cache attributes, names and pages; the timeout
        # follows the refresh interval so a live analysis still shows up
        kernel_cache=True,
        attr_timeout=max(1.0, refresh),
        entry_timeout=max(1.0, refresh),
        negative_timeout=max(1.0, min(refresh, 5.0)),
        fsname="mbkn-btrfs-rescue",
        subtype="mbkn-btrfs-rescue",
    )
