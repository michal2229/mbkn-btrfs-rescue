"""Shared fixtures: build btrfs images with mkfs.btrfs --rootdir (no root needed)."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from mbkn_btrfs_rescue.cli import main
from mbkn_btrfs_rescue.config import load_config

MKFS = shutil.which("mkfs.btrfs")


def pytest_configure(config):
    # Keep test images under the configured tmp_dir instead of /tmp.
    if not config.option.basetemp:
        cfg = load_config()
        base = cfg.tmp_dir / "pytest"
        base.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(base / f"run-{os.getpid()}")


def make_tree(root: Path) -> dict[str, bytes]:
    """Populate a source tree; returns {relative path: content} for regular files."""
    rng = random.Random(1234)
    files = {
        "_work/proj/main.py": b'print("hello")\n' * 3,
        "_work/proj/empty.txt": b"",
        "_work/proj/pkg/big.txt": "".join(
            f"line {i} {rng.random()}\n" for i in range(150_000)
        ).encode(),
        "_work/proj/pkg/rand.bin": rng.randbytes(300_000),
        "_work/proj/zażółć gęślą.md": b"unicode name\n",
        "_work/proj/deep/a/b/c/d.txt": b"deep\n" * 1000,
        "_work/.venv/lib/site.py": b"excluded",
        "_work/proj/.venv-tools/bin/tool": b"excluded too",
        "other/readme.txt": b"another subvolume",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "_work/proj/link").symlink_to("main.py")
    return files


def build_image(
    tmp: Path, compress: str = "no", nodesize: int = 16384, maker=make_tree
) -> tuple[Path, dict]:
    if MKFS is None:
        pytest.skip("mkfs.btrfs not available")
    src = tmp / "src"
    files = maker(src)
    img = tmp / "fs.img"
    with img.open("wb") as fh:
        fh.truncate(512 << 20)
    subprocess.run(
        [
            MKFS,
            "-q",
            "-K",
            "-n",
            str(nodesize),
            "-r",
            str(src),
            "-u",
            "rw:_work",
            "--compress",
            compress,
            str(img),
        ],
        check=True,
    )
    return img, files


@pytest.fixture
def indexed(tmp_path, request):
    compress, nodesize = getattr(request, "param", ("no", 16384))
    img, files = build_image(tmp_path, compress, nodesize)
    db = tmp_path / "index.sqlite"
    base = ["-d", str(img), "--db", str(db)]
    assert main([*base, "scan"]) == 0
    assert main([*base, "extract"]) == 0
    return tmp_path, img, db, files, base
