"""FUSE layout exercised through the operations, without mounting."""

from __future__ import annotations

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import RescueFS

fusefs = pytest.importorskip("mbkn_btrfs_rescue.fusefs")
pytest.importorskip("mfusepy")

ROOT_NAMES = ["README.txt", "best", "intact", "unverified", "damaged", "lost", "all", "history"]


def _ops(img, db, **kw):
    dev = Device(str(img))
    fs = RescueFS(connect(db), dev, exclude=[".venv", ".venv-tools"])
    return fusefs.RescueOps(fs, **kw)


def test_all_and_history_views(indexed):
    _tmp, img, db, files, _ = indexed
    ops = _ops(img, db)
    assert ops.readdir("/", None) == [".", "..", *ROOT_NAMES]
    gens = ops.readdir("/history", None)[2:]
    assert gens and all(g.startswith("gen-") for g in gens)
    work = next(n for n in ops.readdir("/all", None) if n.startswith("_work@"))
    want = files["_work/proj/main.py"]
    for base in ("/all", f"/history/{gens[-1]}"):
        path = f"{base}/{work}/proj/main.py"
        assert ops.getattr(path)["st_size"] == len(want)
        assert ops.read(path, 4096, 0, 0) == want
    assert ".venv" not in ops.readdir(f"/all/{work}", None)
    with pytest.raises(fusefs.fuse.FuseOSError):
        ops.getattr("/history/gen-9999999")
    early = ops.fs.at(0)
    assert early.children(early.resolve(f"/{work}")[-1].node) == {}


def test_category_folders_need_classify(indexed):
    _tmp, img, db, files, base = indexed
    ops = _ops(img, db)
    assert ops.readdir("/intact", None) == [".", ".."]
    assert "run classify" in ops.read("/README.txt", 10000, 0, 0).decode()
    assert main([*base, "classify"]) == 0
    ops = _ops(img, db)
    work = next(n for n in ops.readdir("/intact", None) if n.startswith("_work@"))
    assert ops.read(f"/intact/{work}/proj/main.py", 4096, 0, 0) == files["_work/proj/main.py"]
    assert ops.readdir("/lost", None) == [".", ".."]
    with pytest.raises(fusefs.fuse.FuseOSError):
        ops.getattr(f"/lost/{work}/proj/main.py")
    readme = ops.read("/README.txt", 10000, 0, 0).decode()
    assert "intact/" in readme and "files" in readme


def test_live_refresh_picks_up_new_index(indexed):
    _tmp, img, db, _files, base = indexed
    con = connect(db)
    dev = Device(str(img))
    reloads = []

    def reload():
        reloads.append(1)
        return RescueFS(con, dev)

    ops = fusefs.RescueOps(RescueFS(con, dev), con=con, reload=reload, refresh=0)
    ops.readdir("/", None)
    assert not reloads  # nothing changed yet
    assert main([*base, "classify"]) == 0  # another connection commits
    ops.readdir("/", None)
    assert reloads


def test_flat_mode(indexed):
    _tmp, img, db, _files, _ = indexed
    ops = _ops(img, db, layered=False)
    assert any(n.startswith("_work@") for n in ops.readdir("/", None))


def test_zero_only_files_hidden_outside_lost(indexed):
    _tmp, img, db, files, base = indexed
    con = connect(db)
    work = next(n for n in _ops(img, db).readdir("/all", None) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = con.execute(
        "SELECT child FROM dirents WHERE tree=? AND name=?", (tree, b"main.py")
    ).fetchone()[0]
    con.execute("DELETE FROM extents WHERE tree=? AND ino=?", (tree, ino))  # data gone
    con.commit()
    path = f"/all/{work}/proj/main.py"
    assert "main.py" not in _ops(img, db).readdir(f"/all/{work}/proj", None)
    with pytest.raises(fusefs.fuse.FuseOSError):
        _ops(img, db).getattr(path)
    assert _ops(img, db, show_unreadable=True).getattr(path)["st_size"] == len(
        files["_work/proj/main.py"]
    )
    assert main([*base, "classify"]) == 0
    ops = _ops(img, db)
    assert "main.py" not in ops.readdir(f"/all/{work}/proj", None)
    assert "main.py" in ops.readdir(f"/lost/{work}/proj", None)


def test_best_falls_back_to_older_good_version(tmp_path, indexed):
    """The newest version of a file is broken; best/ serves the older intact one."""
    _tmp, img, db, files, base = indexed
    con = connect(db)
    work = next(n for n in _ops(img, db).readdir("/all", None) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = con.execute(
        "SELECT child FROM dirents WHERE tree=? AND name=?", (tree, b"main.py")
    ).fetchone()[0]
    rows = con.execute("SELECT * FROM extents WHERE tree=? AND ino=?", (tree, ino)).fetchall()
    assert len(rows) == 1
    newer = list(rows[0])
    cols = [d[0] for d in con.execute("SELECT * FROM extents LIMIT 0").description]
    c = {name: i for i, name in enumerate(cols)}
    # a newer version whose data points at garbage (unchecksummed-bad): shift the extent
    newer[c["egen"]] += 1000
    newer[c["gen_min"]] = newer[c["gen_max"]] = newer[c["gen_max"]] + 1000
    newer[c["etype"]] = 1
    newer[c["comp"]] = 0
    newer[c["inline"]] = None
    newer[c["disk_bytenr"]] = 1 << 50  # unmapped
    newer[c["disk_len"]] = newer[c["nbytes"]] = newer[c["ram_bytes"]] = 4096
    newer[c["eoff"]] = 0
    con.execute(f"INSERT INTO extents VALUES ({','.join('?' * len(cols))})", newer)
    con.commit()
    assert main([*base, "classify"]) == 0
    ops = _ops(img, db)
    assert "main.py" not in ops.readdir(f"/all/{work}/proj", None)  # newest is lost
    assert "main.py" in ops.readdir(f"/lost/{work}/proj", None)
    path = f"/best/{work}/proj/main.py"
    want = files["_work/proj/main.py"]
    assert ops.read(path, 1 << 20, 0, 0)[: len(want)] == want
    assert "1 files use an older version" in ops.read("/README.txt", 10000, 0, 0).decode()


def test_outdated_classification_is_detected(indexed):
    from mbkn_btrfs_rescue.pipeline import classification_outdated

    _tmp, _img, db, _files, base = indexed
    con = connect(db)
    exclude = [".venv", ".venv-tools"]
    assert classification_outdated(con, exclude)  # never classified
    assert main([*base, "classify"]) == 0
    assert not classification_outdated(con, exclude)
    assert classification_outdated(con, [".venv"])  # other exclude list
    con.execute("UPDATE meta SET value='1' WHERE key='check_version'")
    con.commit()
    assert classification_outdated(con, exclude)  # rules changed since
