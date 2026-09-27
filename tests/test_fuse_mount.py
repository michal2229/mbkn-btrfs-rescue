"""Real FUSE mount (skipped without /dev/fuse and fusermount3)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from mbkn_btrfs_rescue.cli import main

pytest.importorskip("mfusepy")
if not (os.path.exists("/dev/fuse") and shutil.which("fusermount3")):
    pytest.skip("FUSE not available", allow_module_level=True)


def test_mount_serves_files_through_the_kernel(indexed, tmp_path):
    _tmp, _img, _db, files, base = indexed
    assert main([*base, "classify"]) == 0
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    cmd = [sys.executable, "-m", "mbkn_btrfs_rescue", *base, "mount", "--no-analyze", str(mnt)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if os.path.ismount(mnt):
                break
            if proc.poll() is not None:
                pytest.fail(proc.stdout.read().decode())
            time.sleep(0.1)
        assert os.path.ismount(mnt)
        assert {"README.txt", "PATCHED.tsv", "all", "best", "patched"} <= set(os.listdir(mnt))
        work = next(n for n in os.listdir(mnt / "best") if n.startswith("_work@"))
        want = files["_work/proj/pkg/big.txt"]
        for folder in ("best", "intact", "all"):
            p = mnt / folder / work / "proj/pkg/big.txt"
            assert p.stat().st_size == len(want)
            assert p.read_bytes() == want
        assert os.readlink(mnt / "best" / work / "proj/link") == "main.py"
        assert ".venv" not in os.listdir(mnt / "all" / work)
        with pytest.raises(OSError):
            (mnt / "all" / work / "new.txt").write_bytes(b"x")  # read-only
        # housekeeping tools see the mount and its process, and never read through it
        from mbkn_btrfs_rescue.tools import local_size, our_mounts, our_processes

        assert str(mnt) in our_mounts()
        assert any(p.pid == proc.pid and p.command.startswith("mount") for p in our_processes())
        assert local_size(mnt) == 0
        assert main(["umount", str(mnt)]) == 0
        proc.wait(timeout=10)
        assert not os.path.ismount(mnt)
    finally:
        subprocess.run(["fusermount3", "-u", str(mnt)], check=False, capture_output=True)
        proc.wait(timeout=10)
