"""Scan: chunking, read-ahead and resume must find exactly the same tree blocks."""

from __future__ import annotations

import pytest

from mbkn_btrfs_rescue.db import connect, get_meta
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.scan import ScanParams, scan

from .conftest import build_image


def _blocks(db) -> set[tuple]:
    return set(connect(db).execute("SELECT phys, bytenr, gen, owner, csum_ok FROM nodes"))


@pytest.fixture
def image(tmp_path):
    img, _files = build_image(tmp_path)
    return tmp_path, img


def _scan(img, db, **kw) -> int:
    with Device(str(img)) as dev:
        return scan(dev, connect(db), ScanParams(**kw), progress=False)


def test_chunk_size_does_not_change_results(image):
    tmp, img = image
    _scan(img, tmp / "big.sqlite")
    # tiny chunks put many tree blocks across chunk boundaries
    _scan(img, tmp / "small.sqlite", chunk_bytes=64 << 10)
    ref = _blocks(tmp / "big.sqlite")
    assert ref and _blocks(tmp / "small.sqlite") == ref


def test_split_and_resumed_scans_match_one_pass(image):
    tmp, img = image
    _scan(img, tmp / "one.sqlite")
    size = img.stat().st_size
    mid = (size // 2) // 4096 * 4096
    _scan(img, tmp / "two.sqlite", end=mid, chunk_bytes=1 << 20)
    _scan(img, tmp / "two.sqlite", start=mid, chunk_bytes=1 << 20)
    assert _blocks(tmp / "two.sqlite") == _blocks(tmp / "one.sqlite")

    calls = []

    def stop_after_three(lo, hi, end):
        calls.append(hi)
        if len(calls) == 3:
            raise KeyboardInterrupt

    db = tmp / "resume.sqlite"
    with Device(str(img)) as dev, pytest.raises(KeyboardInterrupt):
        scan(
            dev,
            connect(db),
            ScanParams(chunk_bytes=1 << 20),
            progress=False,
            on_chunk=stop_after_three,
        )
    assert int(get_meta(connect(db), "scan_pos")) == calls[-1]
    assert get_meta(connect(db), "scan_done") is None
    with Device(str(img)) as dev:
        scan(dev, connect(db), ScanParams(chunk_bytes=1 << 20), resume=True, progress=False)
    assert get_meta(connect(db), "scan_done") == "1"
    assert _blocks(db) == _blocks(tmp / "one.sqlite")
