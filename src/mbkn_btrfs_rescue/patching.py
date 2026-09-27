"""Reconstructions best/ serves when no version of a file is fully readable.

Runs after categorising (so the best version of every file is known). For each file whose best
version is damaged or lost, the most complete content is chosen, in this order:

    1. git blob identical to the lost file      (all comparable sectors match its checksums)
    2. best version, all bad ranges filled      (good data of older versions)
    3. git blob of the same size                (staged, stashed or committed)
    4. git blob of another size                 (an older committed version)
    5. best version, some bad ranges filled

Files served verified (intact/unverified best versions, sectors from copies included) are never
touched. The choice is stored in `file_patch` (git content in `git_blob`), so the mount and
`restore` serve it without git; `patched/` in the mount lists exactly these files and
PATCHED.tsv says where each came from.
"""

from __future__ import annotations

import json
import sys
import time
import zlib
from pathlib import Path

from .db import set_meta
from .gitrescue import git_matches
from .model import CAT_DAMAGED, FilePatch, RescueFS

GIT_RANK = {"verified": 1, "same size": 3, "size differs": 4}
RANK_FILLED, RANK_PARTIAL = 2, 5


def compute_patches(
    fs: RescueFS, scratch: Path | None = None, git: bool = True, progress: bool = True
) -> dict[str, int]:
    """Choose and store the reconstruction of every damaged/lost file. Returns counts."""
    con = fs.con
    chosen: dict[tuple[int, int], tuple[int, FilePatch, bytes | None]] = {}
    t0 = time.monotonic()
    best = {(t, i): (g, c) for t, i, g, c in con.execute("SELECT * FROM best_version")}
    damaged = []
    for t, i, cat, _size in con.execute("SELECT * FROM file_cat"):
        gen, bcat = best.get((t, i), (None, cat))
        if bcat == CAT_DAMAGED:
            damaged.append((t, i, gen))
    for n, (t, i, gen) in enumerate(damaged, 1):
        view = fs.view_at(gen)
        fills, left = view.older_fills(t, i)
        if not fills:
            continue
        filled = sum(b - a for a, b, _g in fills)
        detail = f"{filled} bad bytes filled from older versions" + (
            f", {left} still bad" if left else ""
        )
        patch = FilePatch("older", left == 0, detail, gen, fills)
        chosen[(t, i)] = (RANK_FILLED if left == 0 else RANK_PARTIAL, patch, None)
        if progress and n % 100 == 0:
            _progress(f"patching: damaged files {n}/{len(damaged)}")
    for m in git_matches(fs, "/", scratch=scratch, progress=progress) if git else ():
        if m.best is None:
            continue
        rank = GIT_RANK[m.check]
        key = (m.entry.node.tree, m.entry.node.ino)
        if key in chosen and chosen[key][0] <= rank:
            continue
        c = m.best
        detail = f"git {c.source}: {m.check}"
        if c.compared:
            detail += f" ({c.matched}/{c.compared} sectors match the lost file's checksums)"
        chosen[key] = (rank, FilePatch(f"git:{c.source}", True, detail, sha=c.sha), c.data)

    con.execute("DELETE FROM file_patch")
    con.execute("DELETE FROM git_blob")
    blobs = {}
    rows = []
    for (t, i), (_rank, p, data) in chosen.items():
        rows.append(
            (t, i, p.source, int(p.complete), p.detail, p.base_gen,
             json.dumps(p.fills) if p.fills else None, p.sha)
        )  # fmt: skip
        if data is not None:
            blobs[p.sha] = zlib.compress(data, 6)
    con.executemany("INSERT INTO file_patch VALUES (?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO git_blob VALUES (?,?)", blobs.items())
    set_meta(con, "file_patch_serial", f"{int(time.time())}-{len(rows)}")
    con.commit()
    fs._file_patches = None
    counts = {"older": 0, "older_partial": 0, "git": 0}
    for _rank, p, _d in chosen.values():
        if p.source == "older":
            counts["older" if p.complete else "older_partial"] += 1
        else:
            counts["git"] += 1
    if progress:
        print(
            f"\r  patching: {counts['older']} damaged files fully filled from older versions, "
            f"{counts['older_partial']} partly, {counts['git']} files from git "
            f"({time.monotonic() - t0:.0f}s)",
            file=sys.stderr,
        )
    return counts


def _progress(msg: str) -> None:
    print(f"\r  {msg} ", end="", file=sys.stderr, flush=True)
