"""Unit tests for the pure algorithms (no btrfs image needed)."""

from __future__ import annotations

import random
import sqlite3

import numpy as np

from mbkn_btrfs_rescue import ondisk as od
from mbkn_btrfs_rescue.model import ExtentRow, _CsumIndex, paint


def _row(foff: int, length: int, tag: int) -> ExtentRow:
    return ExtentRow(
        foff, tag, od.FILE_EXTENT_REG, 0, 0, length, tag, length, 0, length, None, 0, 0
    )


def _paint_reference(rows):
    """Byte-by-byte painting: the definition paint() must match."""
    owner = {}
    for r in rows:
        for x in range(r.foff, r.foff + r.length):
            owner[x] = r
    segs, cur = [], None
    for x in sorted(owner):
        r = owner[x]
        if cur and cur[1] == x and cur[2] is r:
            cur[1] = x + 1
        else:
            cur = [x, x + 1, r]
            segs.append(cur)
    return [tuple(s) for s in segs]


def _merge(segs):
    """Join adjacent segments of the same row (paint may split them)."""
    out = []
    for a, b, r in segs:
        if out and out[-1][1] == a and out[-1][2] is r:
            out[-1] = (out[-1][0], b, r)
        else:
            out.append((a, b, r))
    return out


def test_paint_matches_reference_on_random_overlaps():
    rng = random.Random(7)
    for _ in range(300):
        rows = [
            _row(rng.randrange(0, 200), rng.randrange(0, 60), i)
            for i in range(rng.randrange(1, 30))
        ]
        got = paint(rows)
        assert all(a < b for a, b, _ in got)
        assert all(got[i][1] <= got[i + 1][0] for i in range(len(got) - 1))
        assert _merge(got) == _paint_reference(rows)


def test_paint_is_fast_for_many_extents():
    import time

    rows = [_row(i * 4096, 8192, i) for i in range(30000)]  # every extent overlaps the next
    t = time.monotonic()
    segs = paint(rows)
    assert time.monotonic() - t < 2
    assert len(segs) == 30000 and segs[-1][1] == 30001 * 4096


def _csum_index(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE nodes (fsid, owner, level, csum_ok, first_off, last_off, gen, phys)")
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,0,1,?,?,?,?)",
        [(b"f", od.CSUM_TREE, a, b, g, p) for a, b, g, p in rows],
    )
    return _CsumIndex(con, b"f"), con


def test_csum_index_matches_sql_semantics():
    rng = random.Random(3)
    rows = []
    for p in range(2000):
        a = rng.randrange(0, 1 << 34)
        span = rng.choice([4096, 1 << 20, 1 << 30])  # some wide leaves
        rows.append((a, a + rng.randrange(0, span), rng.randrange(1, 50), p * 16384))
    idx, con = _csum_index(rows)
    for _ in range(300):
        lo = rng.randrange(0, 1 << 34)
        hi = lo + rng.randrange(0, 1 << 22)
        gen = rng.randrange(1, 50)
        want = con.execute(
            "SELECT phys, gen FROM nodes WHERE first_off <= ? AND last_off >= ? AND gen >= ? "
            "ORDER BY gen, phys LIMIT 64",
            (hi, lo, gen),
        ).fetchall()
        assert idx.find(lo, hi, gen) == [tuple(r) for r in want]


def test_csum_index_empty():
    idx, _ = _csum_index([])
    assert idx.find(0, 1 << 40, 0) == []
    assert isinstance(idx.first, np.ndarray)


def test_extent_check_counts_only_referenced_sectors():
    import zlib

    from mbkn_btrfs_rescue.model import ExtentCheck

    states = bytes([0, 0, 1, 2, 0, 0, 0, 0])
    c = ExtentCheck(5, 1, 1, 0, 1, zlib.compress(states))
    assert c.counts([(0, 8)], 8) == (5, 1, 1)  # whole extent
    assert c.counts([(0, 2), (4, 8)], 8) == (6, 0, 0)  # the bad/unsummed middle is unused
    assert c.counts([(2, 4)], 8) == (0, 1, 1)
    assert c.counts([(-3, 1), (7, 99)], 8) == (2, 0, 0)  # clipped
    uniform = ExtentCheck(0, 8, 0, 8, 1)
    assert uniform.counts([(1, 3)], 8) == (0, 2, 0)
    legacy = ExtentCheck(5, 1, 1, 0, 1)  # mixed without per-sector data: whole extent
    assert legacy.counts([(0, 2)], 8) == (5, 1, 1)


def test_old_index_is_migrated(tmp_path):
    from mbkn_btrfs_rescue.db import connect

    db = tmp_path / "old.sqlite"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE extent_status (disk_bytenr INTEGER, disk_len INTEGER, egen INTEGER, "
        "good INTEGER, bad INTEGER, nocsum INTEGER, zero INTEGER, mapped INTEGER, "
        "PRIMARY KEY (disk_bytenr, disk_len, egen))"
    )
    old.execute("INSERT INTO extent_status VALUES (1, 4096, 5, 1, 0, 0, 0, 1)")
    old.commit()
    old.close()
    con = connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(extent_status)")]
    assert cols[-1] == "sectors"
    assert con.execute("SELECT good, sectors FROM extent_status").fetchone() == (1, None)
    connect(db)  # idempotent


def test_sniff_file_types():
    from mbkn_btrfs_rescue.sniff import plausible

    garbage = random.Random(1).randbytes(4096)
    assert plausible("a.png", b"\x89PNG\r\n\x1a\n" + garbage) is True
    assert plausible("a.png", garbage) is False
    assert plausible("clip.mp4", b"\0\0\0\x20ftypisom" + garbage) is True
    assert plausible("song.wav", b"RIFF\0\0\0\0WAVEfmt ") is True
    assert plausible("notes.md", "zażółć gęślą\n".encode() * 300) is True
    assert plausible("notes.md", "ż".encode() * 3 + "ż".encode()[:1]) is True  # cut mid-char
    assert plausible("main.py", garbage) is False
    assert plausible("main.py", b"print(1)\0\0") is False
    assert plausible(".gitignore", b"*.pyc\n") is True
    assert plausible("data.bin", garbage) is None  # unknown type: no opinion
    assert plausible("x.png", b"") is None


def test_range_helpers():
    from mbkn_btrfs_rescue.model import _intersect, _subtract

    assert _subtract([(0, 100)], [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
    assert _subtract([(0, 10)], [(0, 10)]) == []
    # a bad range filled only where the older version has data (a hole in between)
    assert _intersect([(0, 100)], [(0, 30), (70, 90)]) == [(0, 30), (70, 90)]
    assert _intersect([(10, 20), (40, 80)], [(0, 50)]) == [(10, 20), (40, 50)]
