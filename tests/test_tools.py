"""Housekeeping tools: process matching, sizes, clean (no device needed)."""

from __future__ import annotations

from pathlib import Path

from mbkn_btrfs_rescue.config import Config
from mbkn_btrfs_rescue.tools import _is_ours, clean_items, local_size, remove, untouched

PY = "/usr/bin/python3.14"


def test_only_this_tool_matches():
    assert _is_ours([PY, "/x/.venv/bin/mbkn-btrfs-rescue", "mount", "/m"], PY)
    assert _is_ours([PY, "-m", "mbkn_btrfs_rescue", "status"], PY)
    # an editor opened on the project directory: one space-joined command line
    assert not _is_ours(["/opt/codium /home/u/Work/mbkn-btrfs-rescue"], "/opt/codium")
    assert not _is_ours(["/opt/codium", "/home/u/Work/mbkn-btrfs-rescue"], "/opt/codium")
    assert not _is_ours(["uv", "run", "mbkn-btrfs-rescue"], "/usr/bin/uv")
    assert not _is_ours([PY, "-m", "pytest"], PY)


def test_clean_removes_only_what_the_tool_creates(tmp_path):
    cfg = Config(tmp_dir=tmp_path / "tmp", cache_dir=tmp_path / "cache")
    cfg.prepare_dirs()
    (cfg.tmp_dir / "pytest" / "run-1").mkdir(parents=True)
    (cfg.tmp_dir / "pytest" / "run-1" / "fs.img").write_bytes(b"x" * 10000)
    (cfg.tmp_dir / "git-rescue-abc" / ".git").mkdir(parents=True)
    (cfg.tmp_dir / "analyze.log").write_text("log")
    (cfg.tmp_dir / "recovered").mkdir()  # user data
    (cfg.tmp_dir / "recovered" / "code.py").write_text("keep me")
    (cfg.tmp_dir / "git-rescue-report.tsv").write_text("user's report")
    hashes = cfg.cache_dir / "sector-hashes-x-1-4096.npy"
    hashes.write_bytes(b"h")

    items = clean_items(cfg)
    assert {i.path.name for i in items} == {"pytest", "git-rescue-abc", "analyze.log"}
    assert local_size(cfg.tmp_dir / "pytest") >= 10000
    assert {p.name for p in untouched(cfg, items)} == {"recovered", "git-rescue-report.tsv"}
    assert remove(items) == []
    assert (cfg.tmp_dir / "recovered" / "code.py").read_text() == "keep me"
    assert not (cfg.tmp_dir / "pytest").exists() and hashes.exists()
    assert [i.path for i in clean_items(cfg, hashes=True)] == [hashes]


def test_local_size_does_not_follow_symlinks(tmp_path: Path):
    big = tmp_path / "elsewhere"
    big.mkdir()
    (big / "f").write_bytes(b"x" * 100000)
    d = tmp_path / "d"
    d.mkdir()
    (d / "link").symlink_to(big)
    assert local_size(d) < 100000
