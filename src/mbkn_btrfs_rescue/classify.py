"""Pass 3: verify every data extent against btrfs checksums and categorise every file.

Results are stored in the index (extent_status, file_cat, node_mask), so browsing, mounting
and restoring can show files grouped by category without touching the data again.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from .db import get_meta, set_meta
from .model import (
    CATEGORIES,
    CHECK_VERSION,
    ROOT_INO,
    DataError,
    Node,
    RescueFS,
    classify_key,
    patch_serials,
)


def _progress(msg: str) -> None:
    print(f"\r  {msg} ", end="", file=sys.stderr, flush=True)


DROP_EVERY = 256 << 20


def classify_extents(
    fs: RescueFS,
    trees: set[int] | None = None,
    quick: bool = False,
    progress: bool = True,
    report: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Check all not-yet-checked data extents in physical order. Returns (extents, bytes)."""
    con = fs.con
    mode = "quick" if quick else "full"
    if get_meta(con, "classify_mode") not in (None, mode) and not quick:
        con.execute("DELETE FROM extent_status")  # upgrade sampled results to full ones
    set_meta(con, "classify_mode", mode)
    where = ""
    params: tuple = ()
    if trees:
        where = f" AND tree IN ({','.join('?' * len(trees))})"
        params = tuple(trees)
    done = {
        (b, n, g)
        for b, n, g in con.execute(
            "SELECT disk_bytenr, disk_len, egen FROM extent_status"
            + (
                ""
                if int(get_meta(con, "check_version", "1")) >= CHECK_VERSION
                # results of older rules: re-check mixed extents (now kept per sector) and
                # extents with unchecksummed sectors (now all-zero ones count as bad)
                else " WHERE nocsum = 0 AND (good > 0) + (bad > 0) <= 1"
            )
        )
    }
    todo = []
    for b, n, g, comp in con.execute(
        "SELECT disk_bytenr, disk_len, egen, max(comp) FROM extents "
        "WHERE etype=1 AND disk_bytenr!=0" + where + " GROUP BY disk_bytenr, disk_len, egen",
        params,
    ):
        if (b, n, g) in done:
            continue
        try:
            phys = fs.map_logical(b)
        except DataError:
            phys = []
        todo.append((phys[0] if phys else -1, b, n, g, comp))
    todo.sort()
    total = sum(t[2] for t in todo)
    t0 = last = time.monotonic()
    done_bytes = 0
    batch = []
    dropped = 0  # device page cache behind this offset is released (read-ahead included)
    for i, (at, b, n, g, comp) in enumerate(todo, 1):
        if at - dropped > DROP_EVERY:
            fs.dev.drop_cache(dropped, at - dropped)
            dropped = at
        c = fs.check_extent_raw(b, n, g, quick=quick, comp=comp)
        batch.append((b, n, g, *c[:6]))
        done_bytes += n
        now = time.monotonic()
        if len(batch) >= 5000 or i == len(todo) or now - last > 2:
            con.executemany(
                "INSERT OR REPLACE INTO extent_status VALUES (?,?,?,?,?,?,?,?,?)", batch
            )
            con.commit()
            batch.clear()
        if (progress or report) and (now - last > 2 or i == len(todo)):
            last = now
            rate = done_bytes / max(now - t0, 1e-6)
            eta = (total - done_bytes) / rate if rate else 0
            msg = (
                f"extents {i}/{len(todo)}  {done_bytes / 2**30:.1f}/{total / 2**30:.1f} GiB"
                f"  {rate / 2**20:.0f} MiB/s  ETA {eta / 60:.1f} min"
            )
            if progress:
                _progress(msg)
            if report:
                report(f"classifying: {msg}")
    if progress and todo:
        print(file=sys.stderr)
    if not quick and not trees:  # every extent now follows the current rules
        set_meta(con, "check_version", CHECK_VERSION)
        con.commit()
    fs._ext_status = None  # reload with the stored results
    return len(todo), total


def classify_files(fs: RescueFS, progress: bool = True) -> dict[str, tuple[int, int]]:
    """Compute file categories and directory masks for the latest view and store them."""
    view = fs.at(None)
    view._masks = {}
    view._cats = {}
    view._best = {}
    view._file_stats = {}
    view._file_patches = None
    view._gen_views = type(view._gen_views)(64)
    for tree, name, count in view.trees():
        t0 = time.monotonic()
        if progress:
            _progress(f"categorising /{name}@{tree} ({count} inodes)")
        view.dir_mask(Node(tree, ROOT_INO))
        if progress:
            print(f"done in {time.monotonic() - t0:.0f}s", file=sys.stderr)
    view.save_classification()
    return view.category_totals()


def print_match(stats: dict[str, int]) -> None:
    print(
        f"copies: {stats['copied']} bad sectors recovered from confirmed copies, "
        f"{stats['zero']} known to be zeros (of {stats['bad']} bad sectors; "
        f"{stats['unconfirmed']} with unconfirmed chance matches ignored, "
        f"{stats['no_csum']} without an expected checksum)",
        file=sys.stderr,
    )


def categorise(
    fs: RescueFS, scratch: Path | None = None, patch: bool = True, progress: bool = True
) -> dict[str, tuple[int, int]]:
    """Categorise files, then (with `patch`) choose reconstructions for damaged/lost files
    (patching.py) and categorise again so the mount's masks include them."""
    from .current import build_current

    build_current(fs, progress)
    totals = classify_files(fs, progress)
    if patch:
        from .patching import compute_patches

        had = fs.con.execute("SELECT count(*) FROM file_patch").fetchone()[0]
        if sum(compute_patches(fs, scratch, progress=progress).values()) or had:
            totals = classify_files(fs, progress)  # masks must include the patched files
        else:  # nothing patched: the stored categories stay valid for the new patch serial
            set_meta(fs.con, "classify_exclude", classify_key(fs.exclude, patch_serials(fs.con)))
            fs.con.commit()
    return totals


def classify(
    fs: RescueFS,
    trees: set[int] | None = None,
    quick: bool = False,
    progress: bool = True,
    match_dir: Path | None = None,
    scratch: Path | None = None,
    patch: bool = True,
) -> dict[str, tuple[int, int]]:
    """Check extents, then (with `match_dir`: the sector-hash cache) recover bad sectors from
    copies, then categorise files and choose reconstructions (`patch`)."""
    n, total = classify_extents(fs, trees, quick, progress)
    if progress:
        print(f"checked {n} extents ({total / 2**30:.1f} GiB)", file=sys.stderr)
    if match_dir is not None and not quick:
        from .match import run_match

        stats = run_match(fs, match_dir, progress)
        if progress:
            print_match(stats)
    totals = categorise(fs, scratch, patch=patch and not quick, progress=progress)
    return {c: totals.get(c, (0, 0)) for c in CATEGORIES}
