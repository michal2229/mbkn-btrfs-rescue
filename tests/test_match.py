"""Recovery of bad sectors from identical copies elsewhere on the device (issue #1)."""

from __future__ import annotations

import random
from pathlib import Path

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import ROOT, RescueFS

from .conftest import build_image

EXCLUDE = [".venv", ".venv-tools"]


def _tree(root: Path) -> dict[str, bytes]:
    rng = random.Random(77)
    blob = rng.randbytes(200_000)
    text = "".join(f"{i} {rng.random()}\n" for i in range(300)).encode()[:4096]
    files = {
        "_work/orig.bin": blob,  # damaged below; an identical copy exists
        "_work/backup/copy.bin": blob,
        "_work/zeros.bin": bytes(64 << 10),  # damaged below; content known to be zeros
        "_work/note.txt": text,  # one sector: a copy cannot be confirmed by a neighbour
        "_work/backup/note.txt": text,
        "_work/alone.bin": rng.randbytes(50_000),  # damaged below; no copy anywhere
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def _setup(tmp_path, damage: tuple[str, ...]):
    img, files = build_image(tmp_path, maker=_tree)
    db = tmp_path / "index.sqlite"
    base = ["-d", str(img), "--db", str(db)]
    assert main([*base, "scan"]) == 0 and main([*base, "extract"]) == 0
    con = connect(db)
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    inos = {}
    spans = []
    for name in files:
        e = fs.resolve(f"/{work}/{name.removeprefix('_work/')}")[-1]
        inos[name] = (e.node.tree, e.node.ino)
        if name in damage:
            for _a, _b, r in fs.layout(*inos[name]).segments:
                assert r.is_data, name
                spans.append((fs.map_logical(r.disk_bytenr)[0], r.disk_len))
    with img.open("r+b") as fh:  # test image only: random bytes, like TRIM under LUKS
        rng = random.Random(3)
        for phys, length in spans:
            fh.seek(phys)
            fh.write(rng.randbytes(length))
    return img, db, base, files, inos


def test_bad_sectors_are_recovered_from_copies(tmp_path):
    damaged = ("_work/orig.bin", "_work/zeros.bin", "_work/note.txt", "_work/alone.bin")
    img, db, base, files, inos = _setup(tmp_path, damaged)
    assert main([*base, "classify", "--no-match"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    assert {n: fs.file_check(*inos[n])[0] for n in damaged} == dict.fromkeys(damaged, "lost")

    assert main([*base, "match"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    cats = {n: fs.file_check(*inos[n]) for n in files}
    assert cats["_work/orig.bin"][0] == "intact", cats
    assert "recovered from copies" in cats["_work/orig.bin"][1]
    assert cats["_work/zeros.bin"][0] == "intact", cats
    # a lone sector: with crc32c a match could be chance (a few % on large disks) - not used
    assert cats["_work/note.txt"][0] == "lost", cats
    assert cats["_work/alone.bin"][0] == "lost", cats
    for name in ("_work/orig.bin", "_work/zeros.bin"):
        want = files[name]
        assert fs.read(*inos[name], 0, len(want) + 1) == want, name
    # the copies themselves are untouched
    assert fs.file_check(*inos["_work/backup/copy.bin"])[0] == "intact"


def test_classify_matches_by_default_and_restore_uses_copies(tmp_path):
    img, db, base, files, inos = _setup(tmp_path, ("_work/orig.bin",))
    assert main([*base, "classify"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    assert fs.file_check(*inos["_work/orig.bin"])[0] == "intact"
    work = fs.tree_label(inos["_work/orig.bin"][0])
    out = tmp_path / "out"
    assert main([*base, "restore", f"/{work}/orig.bin", str(out)]) == 0
    assert (out / "orig.bin").read_bytes() == files["_work/orig.bin"]


def test_match_without_bad_sectors_changes_nothing(tmp_path):
    img, db, base, files, inos = _setup(tmp_path, ())
    assert main([*base, "classify"]) == 0
    con = connect(db)
    assert con.execute("SELECT count(*) FROM sector_patch").fetchone()[0] == 0
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    assert all(fs.file_check(*inos[n])[0] == "intact" for n in files)


def test_chance_matches_are_not_used(tmp_path):
    """A lone matching device sector is ignored; a matching pair is checked by re-reading."""
    import numpy as np

    from mbkn_btrfs_rescue.match import match_copies
    from mbkn_btrfs_rescue.sectorhash import csum_key

    img, db, base, _files, inos = _setup(tmp_path, ("_work/alone.bin",))
    assert main([*base, "classify", "--no-match"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    row = fs.layout(*inos["_work/alone.bin"]).segments[0][2]
    sums = fs._data_csums(row.disk_bytenr, 3, row.egen)
    k0, k1 = (csum_key(s, fs.csum_type) for s in sums[:2])
    nsect = fs.dev.size // fs.sectorsize
    fake = np.arange(nsect, dtype=np.uint32) + 7  # no real matches anywhere

    lone = fake.copy()
    lone[1000] = k0  # sector 0 "found", but its neighbour is not next to it
    lone[5000] = k1
    stats = match_copies(fs, lone, progress=False)
    assert stats["copied"] == 0 and stats["stale"] == 0 and stats["unconfirmed"] == 2

    pair = fake.copy()
    pair[1000], pair[1001] = k0, k1  # confirmed layout, but the data there is different
    stats = match_copies(fs, pair, progress=False)
    assert stats["copied"] == 0 and stats["stale"] == 2
