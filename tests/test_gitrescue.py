"""Lost work-tree files recovered from their repository's objects (issue #3)."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.db import connect
from mbkn_btrfs_rescue.device import Device
from mbkn_btrfs_rescue.model import ROOT, RescueFS

from .conftest import build_image

if shutil.which("git") is None:
    pytest.skip("git not available", allow_module_level=True)

EXCLUDE = [".venv", ".venv-tools"]


def _src(n: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return "".join(f"x_{i} = {rng.random()}\n" for i in range(n)).encode()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _tree(root: Path) -> dict[str, bytes]:
    repo = root / "_work/repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    files = {"a.py": _src(400, 1), "b.py": _src(400, 2), "c.py": _src(400, 3)}
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    files["b.py"] = _src(600, 4)  # modified, not staged: only the older version is in git
    files["c.py"] = _src(500, 5)  # modified and staged: the index has it
    for name in ("b.py", "c.py"):
        (repo / name).write_bytes(files[name])
    _git(repo, "add", "c.py")
    files["d.py"] = _src(300, 6)  # never added
    (repo / "d.py").write_bytes(files["d.py"])
    (repo / ".git/hooks/post-checkout").write_text("#!/bin/sh\ntouch /tmp/SHOULD-NOT-RUN\n")
    return {f"_work/repo/{k}": v for k, v in files.items()}


def test_lost_files_come_back_from_git(tmp_path):
    img, files = build_image(tmp_path, maker=_tree)
    db = tmp_path / "index.sqlite"
    base = ["-d", str(img), "--db", str(db)]
    assert main([*base, "scan"]) == 0 and main([*base, "extract"]) == 0
    fs = RescueFS(connect(db), Device(str(img)), exclude=EXCLUDE)
    work = next(n for n in fs.children(ROOT) if n.startswith("_work@"))
    spans = []
    for name in ("a.py", "b.py", "c.py", "d.py"):
        e = fs.resolve(f"/{work}/repo/{name}")[-1]
        for _a, _b, r in fs.layout(e.node.tree, e.node.ino).segments:
            spans.append((fs.map_logical(r.disk_bytenr)[0], r.disk_len))
    with img.open("r+b") as fh:  # test image only: the work tree files are discarded
        for phys, length in spans:
            fh.seek(phys)
            fh.write(random.Random(8).randbytes(length))
    assert main([*base, "classify", "--no-match"]) == 0

    out = tmp_path / "out"
    assert main([*base, "git-rescue", f"/{work}", "--dest", str(out)]) == 0
    report = next(out.glob(".mbkn-git-rescue-*.tsv")).read_text().splitlines()
    rows = {r.split("\t")[0].rsplit("/", 1)[1]: r.split("\t") for r in report[1:]}
    assert rows["a.py"][2:4] == ["index", "verified"]
    assert rows["c.py"][2:4] == ["index", "verified"]  # staged content, newer than HEAD
    assert rows["b.py"][3] == "size differs"  # only the committed (older) version exists
    assert rows["d.py"][3:] == ["not tracked", "", "", str(len(files["_work/repo/d.py"])), "none"]
    got = out / work / "repo"
    assert (got / "a.py").read_bytes() == files["_work/repo/a.py"]
    assert (got / "c.py").read_bytes() == files["_work/repo/c.py"]
    assert (got / "b.py").read_bytes() == _src(400, 2)
