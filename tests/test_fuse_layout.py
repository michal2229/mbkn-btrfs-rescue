"""FUSE layout exercised through the operations, without mounting."""

from __future__ import annotations

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import RescueFS

fusefs = pytest.importorskip("mbkn_btrfs_rescue.fusefs")
pytest.importorskip("mfusepy")

ROOT_NAMES = [
    "README.txt",
    "PATCHED.tsv",
    "best",
    "patched",
    "intact",
    "unverified",
    "damaged",
    "lost",
    "all",
    "history",
]


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
    assert "1 older versions used" in ops.read("/README.txt", 10000, 0, 0).decode()


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


def test_current_shows_the_disk_as_mounted(indexed):
    """current/: top-level subvolume as root, _work mounted in place, only current names."""
    import stat as stat_mod

    _tmp, img, db, files, base = indexed
    ops = _ops(img, db)
    assert "current" not in ops.readdir("/", None)  # not built before classify
    con = connect(db)
    tree = con.execute("SELECT DISTINCT tree FROM dirents WHERE name=?", (b"proj",)).fetchone()[0]
    proj = con.execute("SELECT child FROM dirents WHERE name=?", (b"proj",)).fetchone()[0]
    con.execute(  # a name seen only in an old generation (a deleted file)
        "INSERT INTO dirents VALUES (?,?,?,?,1,1,1,1)", (tree, proj, b"deleted.txt", 999999)
    )
    con.commit()
    assert main([*base, "classify"]) == 0
    ops = _ops(img, db)
    assert ops.readdir("/", None)[-1] == "current"
    walked, stack = {}, ["/current"]
    while stack:
        d = stack.pop()
        for name in ops.readdir(d, None)[2:]:
            p = f"{d}/{name}"
            st = ops.getattr(p)
            if stat_mod.S_ISDIR(st["st_mode"]):
                stack.append(p)
            elif stat_mod.S_ISREG(st["st_mode"]):
                walked[p.removeprefix("/current/")] = ops.read(p, st["st_size"] + 1, 0, 0)
    want = {k: v for k, v in files.items() if ".venv" not in k}
    assert walked == want
    assert ops.readlink("/current/_work/proj/link") == "main.py"
    merged = _ops(img, db, show_unreadable=True)
    work = next(n for n in merged.readdir("/all", None) if n.startswith("_work@"))
    assert "deleted.txt" in merged.readdir(f"/all/{work}/proj", None)  # merged: every name
    assert "deleted.txt" not in ops.readdir("/current/_work/proj", None)


def test_current_without_a_superblock_falls_back_to_the_index(indexed):
    """After a new mkfs the old filesystem has no superblock: the index still finds its trees."""
    from mbkn_btrfs_rescue.current import build_current

    _tmp, img, db, _files, _base = indexed
    fs = RescueFS(connect(db), Device(str(img)))
    with_sb = build_current(fs, progress=False)
    names = sorted(fs.con.execute("SELECT tree, dir, name FROM current_dirent"))
    fs.dev.superblocks = lambda: []  # as if overwritten
    without = build_current(fs, progress=False)
    assert with_sb["from_superblock"] == 1 and without["from_superblock"] == 0
    assert sorted(fs.con.execute("SELECT tree, dir, name FROM current_dirent")) == names
    assert with_sb["missing"] == without["missing"] == 0


def test_current_mounts_nested_subvolumes_in_place(tmp_path):
    """current/_work/nested/ is the nested subvolume; merged views list it once, top level."""
    from .conftest import build_image, make_tree

    def tree(root):
        files = make_tree(root)
        (root / "_work/nested").mkdir()
        (root / "_work/nested/inner.txt").write_bytes(b"in a nested subvolume\n")
        return files | {"_work/nested/inner.txt": b"in a nested subvolume\n"}

    img, _files = build_image(tmp_path, maker=tree, extra=("-u", "rw:_work/nested"))
    db = tmp_path / "index.sqlite"
    base = ["-d", str(img), "--db", str(db)]
    assert main([*base, "scan"]) == 0 and main([*base, "extract"]) == 0
    assert main([*base, "classify"]) == 0
    ops = _ops(img, db)
    assert "nested" in ops.readdir("/current/_work", None)
    p = "/current/_work/nested/inner.txt"
    assert ops.read(p, 100, 0, 0) == b"in a nested subvolume\n"
    top = ops.readdir("/all", None)
    assert any(n.startswith("nested@") for n in top)
    work = next(n for n in top if n.startswith("_work@"))
    assert "nested" not in ops.readdir(f"/all/{work}", None)  # not twice
