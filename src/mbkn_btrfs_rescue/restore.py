"""Copy recovered files/directories out to a destination directory, with a report."""

from __future__ import annotations

import contextlib
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ondisk as od
from .model import CAT_INTACT, CAT_UNVERIFIED, Entry, RescueFS


@dataclass
class RestoreStats:
    files: int = 0
    dirs: int = 0
    links: int = 0
    bytes: int = 0
    skipped: int = 0
    problems: int = 0
    skipped_category: int = 0
    older_versions: int = 0
    by_category: dict[str, int] = field(default_factory=dict)


DEFAULT_CATEGORIES = frozenset({CAT_INTACT, CAT_UNVERIFIED})


def restore(
    fs: RescueFS,
    entry: Entry,
    dest: Path,
    *,
    categories: frozenset[str] | set[str] = DEFAULT_CATEGORIES,
    overwrite: bool = False,
    include_stale: bool = False,
    best: bool = True,
    progress: bool = True,
) -> tuple[RestoreStats, Path]:
    """Copy `entry` (file or directory tree) to dest/<name>.

    With `best` (default), each file is written in its best version - the same one the mount
    shows in best/ (an older version when the newest is damaged, lost or empty). Only files
    whose (chosen version's) category is in `categories` are written; the others are listed
    in the TSV report as skipped. Directories left empty by the filter are removed again.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stats = RestoreStats()
    report_path = dest / f".mbkn-restore-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    dir_times: list[tuple[Path, float]] = []
    created: list[Path] = []  # directories this run created (pruned if left empty)
    with report_path.open("w", newline="") as fh:
        rep = csv.writer(fh, delimiter="\t")
        rep.writerow(["path", "type", "size", "category", "action", "tree", "ino", "detail"])
        for rel, e in fs.walk(entry, include_stale=include_stale):
            target = dest / rel
            node = e.node
            if e.is_dir:
                if not target.exists():
                    created.append(target)
                target.mkdir(parents=True, exist_ok=True)
                stats.dirs += 1
                info = fs.inode(node.tree, node.ino) if node.kind == "inode" else None
                if info:
                    dir_times.append((target, info.mtime))
                continue
            src, gen = fs, None
            if e.ftype == od.FT_SYMLINK:
                cat, detail = fs.category(e), ""
            else:
                if best:
                    gen = fs.best_version(node.tree, node.ino)[0]
                    src = fs.view_at(gen)
                cat, detail = src.file_check(node.tree, node.ino)
                if gen is not None:
                    detail = f"older version (generation {gen}); {detail}"
            stats.by_category[cat] = stats.by_category.get(cat, 0) + 1
            kind = "symlink" if e.ftype == od.FT_SYMLINK else "file"
            if cat not in categories:
                stats.skipped_category += 1
                rep.writerow([rel, kind, "", cat, "skipped", node.tree, node.ino, detail])
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                if not overwrite:
                    stats.skipped += 1
                    rep.writerow([rel, kind, "", cat, "exists", node.tree, node.ino, detail])
                    continue
                target.unlink()
            if e.ftype == od.FT_SYMLINK:
                os.symlink(fs.readlink(node.tree, node.ino), target)
                stats.links += 1
                rep.writerow([rel, kind, "", cat, "written", node.tree, node.ino, ""])
                continue
            errors: list[str] = []
            size = 0
            with target.open("wb") as out:
                for chunk in src.iter_content(node.tree, node.ino, errors=errors):
                    out.write(chunk)
                    size += len(chunk)
            stats.older_versions += gen is not None
            info = src.inode(node.tree, node.ino)
            if info:
                os.utime(target, (info.atime, info.mtime))
            if errors:
                stats.problems += 1
                detail = "; ".join([detail, *errors]).strip("; ")
            stats.files += 1
            stats.bytes += size
            rep.writerow([rel, kind, size, cat, "written", node.tree, node.ino, detail])
            if progress and stats.files % 200 == 0:
                print(
                    f"\r  {stats.files} files, {stats.bytes / 2**20:.1f} MiB",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
    for path in sorted(created, key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            path.rmdir()  # only succeeds when empty
    for path, mtime in reversed(dir_times):
        with contextlib.suppress(OSError):
            os.utime(path, (mtime, mtime))
    if progress and stats.files >= 200:
        print(file=sys.stderr)
    return stats, report_path
