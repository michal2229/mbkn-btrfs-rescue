"""End-to-end: mkfs image -> scan -> extract -> browse/restore -> compare with source."""

from __future__ import annotations

import csv
import os

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import RescueFS


def _run(*argv: str) -> int:
    return main(list(argv))


def _work_path(db, img) -> str:
    with Device(str(img)) as dev:
        fs = RescueFS(connect(db), dev)
        return next(f"/{n}@{t}" for t, n, _ in fs.trees() if n == "_work")


@pytest.mark.parametrize(
    "indexed",
    [("no", 16384), ("zstd", 16384), ("zlib", 16384), ("lzo", 16384), ("zstd", 4096)],
    indirect=True,
)
def test_restore_matches_source(indexed):
    tmp, img, db, files, base = indexed
    out = tmp / "out"
    work = _work_path(db, img)
    assert _run(*base, "restore", work, str(out)) == 0
    root = out / work.lstrip("/")
    for rel, data in files.items():
        if not rel.startswith("_work/"):
            continue
        target = root / rel.removeprefix("_work/")
        if ".venv" in rel:
            assert not target.exists(), f"excluded path restored: {rel}"
        else:
            assert target.read_bytes() == data, rel
    assert os.readlink(root / "proj/link") == "main.py"
    assert not (root / ".orphans").exists()
    report = next(out.glob(".mbkn-restore-*.tsv"))
    cats = {r["category"] for r in csv.DictReader(report.open(), delimiter="\t")}
    assert cats == {"intact"}, cats


def test_no_exclude_and_read_api(indexed):
    _tmp, img, db, files, _ = indexed
    with Device(str(img)) as dev:
        fs = RescueFS(connect(db), dev, exclude=[])
        work = _work_path(db, img)
        chain = fs.resolve(f"{work}/.venv/lib/site.py")
        e = chain[-1]
        assert fs.read(e.node.tree, e.node.ino, 0, 100) == b"excluded"
        big = fs.resolve(f"{work}/proj/pkg/big.txt")[-1]
        want = files["_work/proj/pkg/big.txt"]
        assert fs.read(big.node.tree, big.node.ino, 1000, 5000) == want[1000:6000]
        assert fs.verify_file(big.node.tree, big.node.ino) == "intact"


def test_info_and_listing_commands(indexed, capsys):
    _tmp, _img, _db, _files, base = indexed
    for cmd in (["info"], ["fsids"], ["trees"], ["subvols"], ["ls", "-l", "/"]):
        assert _run(*base, *cmd) == 0
    out = capsys.readouterr().out
    assert "_work@" in out and "superblock" in out


def test_missing_device_is_reported(tmp_path, capsys):
    assert _run("-d", str(tmp_path / "nope.img"), "--db", str(tmp_path / "x.sqlite"), "info") == 2
    assert "no such device" in capsys.readouterr().err


def test_corrupted_files_are_categorised_and_skipped(indexed):
    tmp, img, db, files, base = indexed
    work = _work_path(db, img)
    with Device(str(img)) as dev:
        fs = RescueFS(connect(db), dev)
        rnd = fs.resolve(f"{work}/proj/pkg/rand.bin")[-1]
        rnd_rows = [r for _a, _b, r in fs.layout(rnd.node.tree, rnd.node.ino).segments]
        rnd_phys = [(fs.map_logical(r.disk_bytenr)[0], r.disk_len) for r in rnd_rows]
        big = fs.resolve(f"{work}/proj/pkg/big.txt")[-1]
        big_row = fs.layout(big.node.tree, big.node.ino).segments[0][2]
        big_phys = fs.map_logical(big_row.disk_bytenr)[0]
    with img.open("r+b") as fh:  # corrupt the test image (never the real device)
        for phys, length in rnd_phys:  # every extent of rand.bin -> lost
            fh.seek(phys)
            fh.write(b"\xff" * length)
        fh.seek(big_phys)  # one sector of big.txt -> damaged
        fh.write(b"\xff" * 4096)
    assert _run(*base, "classify") == 0
    with Device(str(img)) as dev:
        fs = RescueFS(connect(db), dev, exclude=[".venv", ".venv-tools"])
        assert fs.masks_valid()
        assert fs.verify_file(rnd.node.tree, rnd.node.ino) == "lost"
        assert fs.verify_file(big.node.tree, big.node.ino) == "damaged"
        totals = fs.category_totals()
        assert totals["lost"][0] == 1 and totals["damaged"][0] == 1
    out = tmp / "out-default"
    assert _run(*base, "restore", work, str(out)) == 0
    root = out / work.lstrip("/")
    assert not (root / "proj/pkg/rand.bin").exists()
    assert not (root / "proj/pkg/big.txt").exists()
    assert (root / "proj/main.py").read_bytes() == files["_work/proj/main.py"]
    report = next(out.glob(".mbkn-restore-*.tsv")).read_text()
    assert "proj/pkg/rand.bin\tfile\t\tlost\tskipped" in report
    out2 = tmp / "out-damaged"
    assert _run(*base, "restore", "--include", "damaged", work, str(out2)) == 0
    assert (out2 / work.lstrip("/") / "proj/pkg/big.txt").exists()
    assert not (out2 / work.lstrip("/") / "proj/pkg/rand.bin").exists()


def test_compat_warnings_for_unsupported_features(indexed):
    import dataclasses

    from mbkn_btrfs_rescue import ondisk as od
    from mbkn_btrfs_rescue.compat import check_superblock, device_warnings

    _tmp, img, _db, _files, _ = indexed
    with Device(str(img)) as dev:
        sbs = dev.superblocks()
    assert device_warnings(sbs) == []
    sb = next(s for s in sbs if s.valid_magic)
    odd = dataclasses.replace(
        sb, incompat_flags=sb.incompat_flags | 1 << 7 | 1 << 30, num_devices=2, sectorsize=65536
    )
    text = " ".join(check_superblock(odd))
    assert "raid56" in text and "unknown" in text and "multi-device" in text and "65536" in text
    assert device_warnings([dataclasses.replace(sb, magic=b"x")])[0].startswith("no valid")
    assert od.INCOMPAT_FLAGS[1 << 10] == "metadata_uuid"
