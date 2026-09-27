"""A review list of files that need a human look: unverified and damaged ones.

For each file (in its best version, as best/ and restore show it): category, size, how many
bytes are bad or unverifiable, and where they are. Directories without such files are skipped
using the stored category masks, so a whole-disk review takes seconds.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from typing import TextIO

from . import ondisk as od
from .model import CAT_BITS, CAT_DAMAGED, CAT_LOST, CAT_UNVERIFIED, Entry, RescueFS

DEFAULT_REVIEW = (CAT_UNVERIFIED, CAT_DAMAGED)
MAX_RANGES = 20


def fmt_ranges(ranges: list[tuple[int, int, str]], kind: str) -> str:
    """ "start-end,start-end" of one kind (at most MAX_RANGES, then "+N more")."""
    sel = [(a, b) for a, b, k in ranges if k == kind]
    text = ",".join(f"{a}-{b}" for a, b in sel[:MAX_RANGES])
    if len(sel) > MAX_RANGES:
        text += f",+{len(sel) - MAX_RANGES} more"
    return text


def _files(fs: RescueFS, entry: Entry, rel: str, bits: int) -> Iterator[tuple[str, Entry]]:
    if not entry.is_dir:
        yield rel, entry
        return
    seen = set()
    stack = [(rel, entry)]
    while stack:
        path, e = stack.pop()
        if e.node in seen:
            continue
        seen.add(e.node)
        for name, child in sorted(fs.children(e.node).items(), reverse=True):
            if child.stale:
                continue
            if child.is_dir:
                if fs.entry_mask(child) & bits:
                    stack.append((f"{path}/{name}", child))
            elif child.ftype == od.FT_REG and fs.entry_mask(child) & bits:
                yield f"{path}/{name}", child


def review(
    fs: RescueFS,
    entry: Entry,
    rel: str,
    out: TextIO,
    categories: tuple[str, ...] = DEFAULT_REVIEW,
    best: bool = True,
    header: bool = True,
) -> dict[str, int]:
    """Write a TSV review list for files below `entry`; returns {category: files}."""
    bits = 0
    for c in categories:
        bits |= CAT_BITS[c]
    if best:  # the best version can be better than the latest: look at worse files too
        bits |= CAT_BITS[CAT_DAMAGED] | CAT_BITS[CAT_LOST]
    w = csv.writer(out, delimiter="\t", lineterminator="\n")
    if header:
        w.writerow(
            ["path", "category", "size", "bad_bytes", "unverified_bytes", "version",
             "bad_ranges", "unverified_ranges", "detail", "best_serves"]
        )  # fmt: skip
    counts: dict[str, int] = {}
    for path, e in _files(fs, entry, rel, bits):
        t, i = e.node.tree, e.node.ino
        gen, cat = fs.best_version(t, i) if best else (None, fs.file_check(t, i)[0])
        if cat not in categories:
            continue
        src = fs.view_at(gen)
        detail = src.file_check(t, i)[1]
        ranges = src.problem_ranges(t, i)
        nbad = sum(b - a for a, b, k in ranges if k == "bad")
        nunv = sum(b - a for a, b, k in ranges if k == "unverified")
        patch = fs.file_patch(t, i) if best else None
        serves = f"reconstruction: {patch.detail}" if patch else "this version"
        w.writerow(
            [path, cat, src.layout(t, i).size, nbad, nunv, "latest" if gen is None else gen,
             fmt_ranges(ranges, "bad"), fmt_ranges(ranges, "unverified"), detail, serves]
        )  # fmt: skip
        counts[cat] = counts.get(cat, 0) + 1
    out.flush()
    return counts
