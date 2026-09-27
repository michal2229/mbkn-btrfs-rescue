"""Cross-checks between the mount folders, the categories, best/ and restore.

One image covers every case: intact, damaged (one bad sector), lost (all sectors bad), no data
at all, a newest version that is broken while an older one is intact, and a file truncated to
empty whose older content is intact.
"""

from __future__ import annotations

import random
import stat as stat_mod

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import CATEGORIES, ROOT, RescueFS

fusefs = pytest.importorskip("mbkn_btrfs_rescue.fusefs")
pytest.importorskip("mfusepy")

EXCLUDE = [".venv", ".venv-tools"]
FOLDERS = ("best", *CATEGORIES, "all")


def _ino(con, tree, name: str) -> int:
    return con.execute(
        "SELECT child FROM dirents WHERE tree=? AND name=?", (tree, name.encode())
    ).fetchone()[0]


def _add_newer_broken_version(con, tree, ino):
    """A newer extent over the whole file pointing at an unmapped address (-> lost)."""
    cols = [d[0] for d in con.execute("SELECT * FROM extents LIMIT 0").description]
    c = {n: i for i, n in enumerate(cols)}
    row = list(
        con.execute(
            "SELECT * FROM extents WHERE tree=? AND ino=? ORDER BY egen DESC", (tree, ino)
        ).fetchone()
    )
    row[c["egen"]] += 1000
    row[c["gen_min"]] = row[c["gen_max"]] = row[c["gen_max"]] + 1000
    row[c["etype"]], row[c["comp"]], row[c["inline"]], row[c["eoff"]] = 1, 0, None, 0
    row[c["disk_bytenr"]] = 1 << 50
    row[c["disk_len"]] = row[c["nbytes"]] = row[c["ram_bytes"]] = 4096
    con.execute(f"INSERT INTO extents VALUES ({','.join('?' * len(cols))})", row)


def _truncate_to_empty(con, tree, ino):
    """A newer inode record with size 0 (file truncated when disaster struck)."""
    cols = [d[0] for d in con.execute("SELECT * FROM inodes LIMIT 0").description]
    c = {n: i for i, n in enumerate(cols)}
    row = list(
        con.execute(
            "SELECT * FROM inodes WHERE tree=? AND ino=? ORDER BY gen DESC", (tree, ino)
        ).fetchone()
    )
    row[c["gen"]] += 2000
    row[c["size"]] = 0
    con.execute(f"INSERT INTO inodes VALUES ({','.join('?' * len(cols))})", row)


@pytest.fixture
def scenario(indexed):
    tmp, img, db, files, base = indexed
    con = connect(db)
    with Device(str(img)) as dev:
        fs = RescueFS(con, dev)
        work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
        tree = int(work.split("@")[1])
        rnd = _ino(con, tree, "rand.bin")
        big = _ino(con, tree, "big.txt")
        rnd_phys = [
            (fs.map_logical(r.disk_bytenr)[0], r.disk_len)
            for _a, _b, r in fs.layout(tree, rnd).segments
        ]
        big_row = fs.layout(tree, big).segments[0][2]
        big_phys = fs.map_logical(big_row.disk_bytenr)[0]
    with img.open("r+b") as fh:  # test image only
        for phys, length in rnd_phys:
            fh.seek(phys)
            fh.write(b"\xff" * length)
        fh.seek(big_phys)
        fh.write(b"\xff" * 4096)
    con.execute("DELETE FROM extents WHERE tree=? AND ino=?", (tree, _ino(con, tree, "d.txt")))
    _add_newer_broken_version(con, tree, _ino(con, tree, "main.py"))
    _truncate_to_empty(con, tree, _ino(con, tree, "zażółć gęślą.md"))
    con.commit()
    assert main([*base, "classify"]) == 0
    return tmp, img, db, files, base, work


def _ops(img, db, **kw):
    return fusefs.RescueOps(RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE), **kw)


def _walk(ops, top: str) -> tuple[dict[str, dict], set[str]]:
    """({relative file path: stat}, {relative dir paths}) below a mount folder."""
    files, dirs, stack = {}, set(), [top]
    while stack:
        d = stack.pop()
        for name in ops.readdir(d, None)[2:]:
            p = f"{d}/{name}"
            st = ops.getattr(p)
            rel = p[len(top) :]
            if stat_mod.S_ISDIR(st["st_mode"]):
                dirs.add(rel)
                stack.append(p)
            else:
                files[rel] = st
    return files, dirs


def _read_all(ops, path: str, st: dict) -> bytes:
    if stat_mod.S_ISLNK(st["st_mode"]):
        return ops.readlink(path).encode()
    return ops.read(path, st["st_size"] + 1, 0, 0)


def _folders(ops):
    return {f: _walk(ops, f"/{f}") for f in FOLDERS}


def test_every_file_is_in_exactly_one_category_folder(scenario):
    _tmp, img, db, _files, _base, _work = scenario
    ops = _ops(img, db)
    everything, _ = _walk(_ops(img, db, show_unreadable=True), "/all")
    walked = _folders(ops)
    fs = ops.fs
    for rel in everything:
        homes = [c for c in CATEGORIES if rel in walked[c][0]]
        assert len(homes) == 1, (rel, homes)
        e = fs.resolve(rel)[-1]
        assert homes[0] == fs.category(e), rel
    assert set().union(*(walked[c][0] for c in CATEGORIES)) == set(everything)


def test_folder_counts_match_readme_totals(scenario):
    _tmp, img, db, _files, _base, _work = scenario
    ops = _ops(img, db)
    totals = ops.fs.category_totals()
    for cat in CATEGORIES:
        files, _ = _walk(ops, f"/{cat}")
        assert len(files) == totals[cat][0], cat
    readme = ops.read("/README.txt", 100000, 0, 0).decode()
    assert "2 files use an older version" in readme


def test_expected_categories(scenario):
    _tmp, img, db, _files, _base, work = scenario
    walked = _folders(_ops(img, db))
    proj = f"/{work}/proj"
    assert f"{proj}/pkg/rand.bin" in walked["lost"][0]
    assert f"{proj}/deep/a/b/c/d.txt" in walked["lost"][0]
    assert f"{proj}/main.py" in walked["lost"][0]  # newest version broken
    assert f"{proj}/pkg/big.txt" in walked["damaged"][0]
    assert f"{proj}/zażółć gęślą.md" in walked["intact"][0]  # newest version: empty
    assert f"{proj}/link" in walked["intact"][0]


def test_review_lists_damaged_files_with_their_bad_ranges(scenario, capsys):
    _tmp, _img, _db, _files, base, work = scenario
    capsys.readouterr()
    assert main([*base, "review"]) == 0
    out = capsys.readouterr()
    rows = [line.split("\t") for line in out.out.splitlines()]
    assert rows[0][:3] == ["path", "category", "size"]
    by_path = {r[0]: r for r in rows[1:]}
    big = by_path[f"/{work}/proj/pkg/big.txt"]
    assert big[1] == "damaged" and big[3] == "4096" and big[6] == "0-4096"
    # lost files and files whose best version is fine are not listed
    assert f"/{work}/proj/pkg/rand.bin" not in by_path
    assert f"/{work}/proj/main.py" not in by_path
    assert "1 damaged" in out.err
    assert main([*base, "review", f"/{work}/proj/pkg", "--include", "lost"]) == 0
    assert f"/{work}/proj/pkg/rand.bin" in capsys.readouterr().out


def test_all_hides_exactly_the_lost_files(scenario):
    _tmp, img, db, _files, _base, _work = scenario
    ops = _ops(img, db)
    everything, _ = _walk(_ops(img, db, show_unreadable=True), "/all")
    shown, _ = _walk(ops, "/all")
    lost, _ = _walk(ops, "/lost")
    assert set(shown) == set(everything) - set(lost)


def test_best_serves_the_best_version_of_every_readable_file(scenario):
    _tmp, img, db, files, _base, work = scenario
    ops = _ops(img, db)
    best, _ = _walk(ops, "/best")
    shown, _ = _walk(ops, "/all")
    lost, _ = _walk(ops, "/lost")
    proj = f"/{work}/proj"
    rescued = {f"{proj}/main.py", f"{proj}/zażółć gęślą.md"}
    assert set(best) == set(shown) | {f"{proj}/main.py"}
    assert rescued <= set(best)
    assert f"{proj}/pkg/rand.bin" not in best and f"{proj}/pkg/rand.bin" in lost
    for rel in rescued:
        name = rel.removeprefix(f"/{work}/")
        assert _read_all(ops, f"/best{rel}", best[rel]) == files[f"_work/{name}"]
    for rel, st in best.items():  # everything else in best/ is the version in all/
        if rel not in rescued:
            assert _read_all(ops, f"/best{rel}", st) == _read_all(ops, f"/all{rel}", shown[rel])


def test_sizes_match_content_everywhere(scenario):
    _tmp, img, db, _files, _base, _work = scenario
    ops = _ops(img, db)
    for folder, (files, _dirs) in _folders(ops).items():
        for rel, st in files.items():
            if stat_mod.S_ISREG(st["st_mode"]):
                assert len(_read_all(ops, f"/{folder}{rel}", st)) == st["st_size"], (folder, rel)


def test_category_folders_have_no_empty_directories(scenario):
    _tmp, img, db, _files, _base, _work = scenario
    ops = _ops(img, db)
    for folder in ("best", *CATEGORIES):
        files, dirs = _walk(ops, f"/{folder}")
        for d in dirs:
            if d.count("/") > 1:  # subvolume roots may be empty
                assert any(f.startswith(d + "/") for f in files), (folder, d)


def test_restore_writes_what_best_shows(scenario):
    tmp, img, db, files, base, work = scenario
    ops = _ops(img, db)
    best, _ = _walk(ops, "/best")
    out = tmp / "restored"
    assert main([*base, "restore", f"/{work}", str(out)]) == 0
    written = {
        "/" + str(p.relative_to(out))
        for p in out.rglob("*")
        if (p.is_file() or p.is_symlink()) and not p.name.startswith(".mbkn-restore-")
    }
    good = {
        rel
        for rel in best
        if rel.startswith(f"/{work}/")
        and ops.fs.best_version(*_node(ops.fs, rel))[1] in ("intact", "unverified")
    }
    assert written == good
    for rel in good:
        p = out / rel.lstrip("/")
        want = _read_all(ops, f"/best{rel}", best[rel])
        got = str(p.readlink()).encode() if p.is_symlink() else p.read_bytes()
        assert got == want, rel
    latest = tmp / "restored-latest"
    assert main([*base, "restore", "--latest", f"/{work}", str(latest)]) == 0
    assert not (latest / work / "proj/main.py").exists()  # newest main.py is lost
    assert (latest / work / "proj/zażółć gęślą.md").read_bytes() == b""  # newest is empty
    assert (out / work / "proj/main.py").read_bytes() == files["_work/proj/main.py"]


def _node(fs, rel):
    e = fs.resolve(rel)[-1]
    return e.node.tree, e.node.ino


def test_shell_commands_smoke(scenario, capsys):
    from mbkn_btrfs_rescue.shell import Shell

    tmp, img, db, files, _base, work = scenario
    sh = Shell(RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE))
    for line in (
        f"cd /{work}/proj",
        "ls -a",
        "tree",
        "find main",
        "grep -l unicode",
        "stat main.py",
        "versions main.py",
        "cat link",
        "summary",
        f"restore main.py {tmp / 'sh-out'}",
        "gen 1",
        "gen",
    ):
        sh.onecmd(line)
    out = capsys.readouterr().out
    assert "older version at generation" in out  # stat shows the best/ fallback
    assert "Traceback" not in out
    assert (tmp / "sh-out/main.py").read_bytes() == files["_work/proj/main.py"]


def test_extract_twice_is_idempotent(indexed):
    _tmp, _img, db, _files, base = indexed
    con = connect(db)
    tables = ("inodes", "dirents", "extents", "chunks", "roots", "root_refs")
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    assert main([*base, "extract"]) == 0
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after and before["extents"] > 0


def _extent_row(con, tree, ino):
    cols = [d[0] for d in con.execute("SELECT * FROM extents LIMIT 0").description]
    row = con.execute(
        "SELECT * FROM extents WHERE tree=? AND ino=? ORDER BY foff LIMIT 1", (tree, ino)
    ).fetchone()
    return cols, {n: i for i, n in enumerate(cols)}, list(row)


def _insert(con, cols, row):
    con.execute(f"INSERT INTO extents VALUES ({','.join('?' * len(cols))})", row)


@pytest.mark.parametrize("case", ["reflink", "prealloc"])
def test_newest_metadata_wins_over_newer_data(indexed, case):
    """Content follows the newest leaf that describes a range, not the newest data.

    reflink: the file's latest item points at *older* data (clone/dedupe), while a transient
    item with newer data was only seen in an older leaf. prealloc: fallocate + write in one
    transaction leaves a prealloc and a data item with the same generations.
    """
    _tmp, img, db, files, _base = indexed
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = _ino(con, tree, "rand.bin")
    cols, c, row = _extent_row(con, tree, ino)
    fake = list(row)
    if case == "reflink":
        con.execute(
            "UPDATE extents SET gen_max = gen_max + 100 WHERE tree=? AND ino=?", (tree, ino)
        )
        fake[c["egen"]] += 50  # newer data ...
        fake[c["disk_bytenr"]] = 1 << 50  # ... that is unreadable
    else:
        fake[c["etype"]] = 2  # prealloc, same generations as the data item
    _insert(con, cols, fake)
    con.commit()
    fs = RescueFS(con, Device(str(img)))
    e = fs.resolve(f"/{work}/proj/pkg/rand.bin")[-1]
    want = files["_work/proj/pkg/rand.bin"]
    assert fs.read(tree, e.node.ino, 0, len(want) + 1) == want
    assert fs.file_check(tree, e.node.ino)[0] == "intact"


def test_bad_sector_outside_the_referenced_range_does_not_count(indexed):
    """A file that uses only part of an extent is judged by that part only."""
    _tmp, img, db, files, base = indexed
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = _ino(con, tree, "rand.bin")  # uncompressed, random (incompressible)
    row = fs.layout(tree, ino).segments[0][2]
    assert row.comp == 0 and row.nbytes >= 3 * 4096
    last = fs.map_logical(row.disk_bytenr)[0] + row.disk_len - 4096
    with img.open("r+b") as fh:  # test image only: corrupt the extent's last sector
        fh.seek(last)
        fh.write(b"\xff" * 4096)
    keep = row.disk_len - 4096 - row.eoff  # the file now stops before that sector
    con.execute(
        "UPDATE extents SET nbytes=? WHERE tree=? AND ino=? AND foff=?",
        (keep, tree, ino, row.foff),
    )
    con.execute("UPDATE inodes SET size=? WHERE tree=? AND ino=?", (row.foff + keep, tree, ino))
    con.commit()
    assert main([*base, "classify"]) == 0
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    cat, detail = fs.file_check(tree, ino)
    assert cat == "intact", detail
    c = fs.check_extent(row.disk_bytenr, row.disk_len, row.egen)
    assert c.bad == 1 and c.sectors is not None  # the extent itself is mixed
    want = files["_work/proj/pkg/rand.bin"][row.foff : row.foff + keep]
    assert fs.read(tree, ino, row.foff, keep) == want


def test_unchecksummed_zeros_are_lost_other_unchecksummed_data_unverified(indexed):
    """Without checksums, content is unverified - unless it reads as nothing but zeros."""
    _tmp, img, db, _files, base = indexed
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = _ino(con, tree, "rand.bin")
    spans = [
        (fs.map_logical(r.disk_bytenr)[0], r.disk_len)
        for _a, _b, r in fs.layout(tree, ino).segments
    ]
    with img.open("r+b") as fh:  # test image only
        for phys, length in spans:
            fh.seek(phys)
            fh.write(bytes(length))
    con.execute("DELETE FROM nodes WHERE owner=7")  # checksum tree gone
    con.commit()
    assert main([*base, "classify"]) == 0
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    assert fs.file_check(tree, ino)[0] == "lost"
    big = _ino(con, tree, "big.txt")
    assert fs.file_check(tree, big)[0] == "unverified"
    e = fs.resolve(f"/{work}/proj/pkg/big.txt")[-1]
    assert fs.category(e) == "unverified"  # text that looks like text


def test_unchecksummed_text_with_garbage_start_is_not_unverified(indexed):
    _tmp, img, db, _files, base = indexed
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    big = _ino(con, tree, "big.txt")
    rows = [r for _a, _b, r in fs.layout(tree, big).segments]
    with img.open("r+b") as fh:  # test image only: garbage in the first extent
        fh.seek(fs.map_logical(rows[0].disk_bytenr)[0])
        fh.write(random.Random(9).randbytes(4096))
    con.execute("DELETE FROM nodes WHERE owner=7")
    con.commit()
    assert main([*base, "classify"]) == 0
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    e = fs.resolve(f"/{work}/proj/pkg/big.txt")[-1]
    assert fs.category(e) == ("damaged" if len({r.disk_bytenr for r in rows}) > 1 else "lost")


def test_unchecksummed_compressed_garbage_is_lost(tmp_path):
    """A compressed extent without checksum that does not decompress is garbage (TRIM)."""
    from .conftest import build_image

    img, _files = build_image(tmp_path, "zstd")
    db = tmp_path / "index.sqlite"
    base = ["-d", str(img), "--db", str(db)]
    assert main([*base, "scan"]) == 0 and main([*base, "extract"]) == 0
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = _ino(con, tree, "big.txt")  # compressible: zstd extents
    rows = [r for _a, _b, r in fs.layout(tree, ino).segments]
    assert rows and all(r.comp == 3 for r in rows)
    first = rows[0]
    with img.open("r+b") as fh:  # test image only: random bytes, like TRIM under LUKS
        fh.seek(fs.map_logical(first.disk_bytenr)[0])
        fh.write(random.Random(5).randbytes(first.disk_len))
    con.execute("DELETE FROM nodes WHERE owner=7")  # no checksums at all
    con.commit()
    assert main([*base, "classify"]) == 0
    fs = RescueFS(con, Device(str(img)), exclude=EXCLUDE)
    assert fs.file_check(tree, ino)[0] == "damaged"  # first extent garbage, rest decodes
    main_py = _ino(con, tree, "main.py")
    assert fs.file_check(tree, main_py)[0] == "intact"  # inline


def test_restore_fill_older_fills_bad_ranges_from_an_older_version(indexed):
    """Newest version: first sector bad. Older version: good there, bad in the middle."""
    tmp, img, db, files, base = indexed
    con = connect(db)
    fs = RescueFS(con, Device(str(img)))
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    tree = int(work.split("@")[1])
    ino = _ino(con, tree, "big.txt")
    row = fs.layout(tree, ino).segments[0][2]
    assert row.comp == 0 and row.nbytes > 16 * 4096
    with img.open("r+b") as fh:  # test image only: older version bad in sectors 10-12
        fh.seek(fs.map_logical(row.disk_bytenr)[0] + 10 * 4096)
        fh.write(b"\xff" * 3 * 4096)
    cols = [d[0] for d in con.execute("SELECT * FROM extents LIMIT 0").description]
    c = {n: i for i, n in enumerate(cols)}
    old = list(
        con.execute(
            "SELECT * FROM extents WHERE tree=? AND ino=? AND foff=0", (tree, ino)
        ).fetchone()
    )
    newer = old[c["gen_max"]] + 1000
    for foff, bytenr in ((0, 1 << 50), (10 * 4096, 0)):  # unmapped sector; hole over 10-12
        r = list(old)
        r[c["foff"]], r[c["egen"]], r[c["gen_min"]], r[c["gen_max"]] = foff, newer, newer, newer
        r[c["disk_bytenr"]], r[c["eoff"]] = bytenr, 0
        r[c["disk_len"]] = r[c["ram_bytes"]] = 4096 if bytenr else 3 * 4096
        r[c["nbytes"]] = r[c["disk_len"]]
        con.execute(f"INSERT INTO extents VALUES ({','.join('?' * len(cols))})", r)
    con.commit()
    assert main([*base, "classify", "--no-match"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    assert fs.best_version(tree, ino) == (None, "damaged")  # newest: 1 bad sector, older: 3
    out = tmp / "filled"
    assert main([*base, "restore", "--fill-older", f"/{work}/proj/pkg/big.txt", str(out)]) == 1
    want = bytearray(files["_work/proj/pkg/big.txt"])
    want[10 * 4096 : 13 * 4096] = bytes(3 * 4096)  # the newer version's hole
    assert (out / "big.txt").read_bytes() == bytes(want)
    report = next(out.glob(".mbkn-restore-*.tsv")).read_text()
    assert "filled from older versions: 0-4096@" in report
