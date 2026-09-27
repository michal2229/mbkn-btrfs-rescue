"""Housekeeping: what is running, stop it, and clean up what the tool leaves behind.

    status   mounts, running commands, analysis progress, device access, disk usage
    umount   unmount this tool's FUSE mounts
    stop     unmount everything and pause running analyses (resumable)
    clean    remove test images, scratch directories, logs (and caches on request)

Nothing here ever walks into a mount point: sizes are computed on the local filesystem only
(a FUSE mount of a recovered disk would otherwise be read in full).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import sqlite3
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

FSTYPE = "fuse.mbkn-btrfs-rescue"
BLKROGET = 0x125E


# ---------------------------------------------------------------------------- discovery


def our_mounts() -> list[str]:
    """Mount points of this tool's FUSE mounts (any user)."""
    out = []
    with open("/proc/self/mounts") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) > 2 and parts[2] == FSTYPE:
                out.append(parts[1].replace("\\040", " "))
    return out


@dataclass
class Proc:
    pid: int
    args: list[str]
    seconds: float

    @property
    def command(self) -> str:
        """The subcommand and its first argument, e.g. "mount /mnt/rescue"."""
        names = _subcommands()
        for k, a in enumerate(self.args):
            if a in names:
                rest = [x for x in self.args[k + 1 :] if not x.startswith("-")]
                return " ".join([a, *rest[:1]])
        return "(no command)"


def _subcommands() -> set[str]:
    from .cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        if action.choices and isinstance(action.choices, dict):
            return set(action.choices)
    return set()


def _is_ours(argv: list[str], exe: str) -> bool:
    """This tool itself: the Python interpreter (`exe`, what the kernel runs) running its
    script or `-m mbkn_btrfs_rescue`.

    Deliberately strict: an editor opened on the project directory (whose command line may
    end in ".../mbkn-btrfs-rescue") must never match - `stop` sends it signals."""
    if not Path(exe).name.startswith("python") or len(argv) < 2:
        return False
    if Path(argv[1]).name == "mbkn-btrfs-rescue":
        return True
    return argv[1:3] == ["-m", "mbkn_btrfs_rescue"]


def our_processes() -> list[Proc]:
    """Running mbkn-btrfs-rescue commands (not `uv run` wrappers, not this process)."""
    me = {os.getpid(), os.getppid()}
    boot = time.time() - float(Path("/proc/uptime").read_text().split()[0])
    tick = os.sysconf("SC_CLK_TCK")
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in me:
            continue
        try:
            args = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
        except OSError:
            continue
        argv = [os.fsdecode(a) for a in args if a]
        try:
            exe = os.readlink(d / "exe")
        except OSError:
            continue
        if not _is_ours(argv, exe):
            continue
        start = int(stat.rsplit(")", 1)[1].split()[19]) / tick
        out.append(Proc(int(d.name), argv, time.time() - (boot + start)))
    return sorted(out, key=lambda p: p.pid)


def device_access(path: str | None) -> str:
    if not path:
        return "no device configured"
    if not os.path.exists(path):
        return f"{path}: not present (LUKS mapping closed?)"
    if not os.access(path, os.R_OK):
        return f"{path}: not readable - scripts/device-access.sh grant {path}"
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            ro = struct.unpack("i", fcntl.ioctl(fd, BLKROGET, b"\0" * 4))[0]
        finally:
            os.close(fd)
        return f"{path}: readable, block-layer read-only: {'yes' if ro else 'NO'}"
    except OSError:
        return f"{path}: readable (image file)"


def local_size(path: Path) -> int:
    """Bytes used below `path`, staying on its filesystem and never entering mount points."""
    if os.path.ismount(path):
        return 0
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_blocks * 512
    total, dev = 0, st.st_dev
    for root, dirs, files in os.walk(path):
        keep = []
        for d in dirs:
            p = os.path.join(root, d)
            try:
                if os.lstat(p).st_dev == dev and not os.path.ismount(p):
                    keep.append(d)
            except OSError:
                pass
        dirs[:] = keep
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
    return total


def fmt(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------- status


def status(cfg: Config, db_path: Path) -> str:
    lines = []
    mounts = our_mounts()
    lines.append("mounts:" if mounts else "mounts: none")
    lines += [f"  {m}" for m in mounts]
    procs = our_processes()
    lines.append("running:" if procs else "running: nothing")
    for p in procs:
        lines.append(f"  pid {p.pid:<8} {_age(p.seconds):>8}  {p.command}")
    lines.append(f"analysis: {_analysis(db_path)}")
    lines.append(f"device: {device_access(cfg.device)}")
    lines.append("disk usage:")
    for label, path in _usage_items(cfg, db_path):
        lines.append(f"  {fmt(local_size(path)):>8}  {label:18} {path}")
    return "\n".join(lines)


def _age(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _analysis(db_path: Path) -> str:
    if not db_path.exists():
        return f"no index yet ({db_path})"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        con.close()
    except sqlite3.Error as err:
        return f"index unreadable: {err}"
    state = meta.get("analyze_state") or ("classified" if meta.get("classify_exclude") else "?")
    text = state
    if meta.get("analyze_status") and state != "complete":
        text += f" - {meta['analyze_status']}"
    return text


def _usage_items(cfg: Config, db_path: Path) -> list[tuple[str, Path]]:
    items = [("index", db_path)]
    items += [("sector hashes", p) for p in sorted(cfg.cache_dir.glob("sector-hashes-*.npy"))]
    items.append(("tmp_dir", cfg.tmp_dir))
    return items


# ---------------------------------------------------------------------------- stop


def umount(points: list[str], lazy: bool = False) -> list[tuple[str, str]]:
    """Unmount; returns [(mount point, error)] for the ones that failed."""
    tool = shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount3"
    failed = []
    for m in points:
        r = subprocess.run(
            [tool, "-u", *(["-z"] if lazy else []), m], capture_output=True, text=True, check=False
        )
        if r.returncode:
            failed.append((m, (r.stderr or r.stdout).strip()))
    return failed


def stop(timeout: float = 15.0, force: bool = False) -> list[str]:
    """Unmount all mounts, then interrupt the remaining commands (they resume later)."""
    report = []
    for m, err in umount(our_mounts()):
        report.append(f"could not unmount {m}: {err} (a program is using it; `umount --lazy`)")
    time.sleep(0.5)
    procs = our_processes()
    for p in procs:
        with contextlib.suppress(ProcessLookupError):
            os.kill(p.pid, signal.SIGTERM if force else signal.SIGINT)
    deadline = time.monotonic() + timeout
    while procs and time.monotonic() < deadline:
        time.sleep(0.3)
        procs = [p for p in procs if Path(f"/proc/{p.pid}").exists()]
    report += [f"still running: pid {p.pid} ({p.command}); try --force" for p in procs]
    return report


# ---------------------------------------------------------------------------- clean


@dataclass
class CleanItem:
    label: str
    path: Path
    size: int


def clean_items(cfg: Config, hashes: bool = False) -> list[CleanItem]:
    """What `clean` removes: only things this tool creates and can recreate."""
    tmp, out = cfg.tmp_dir, []
    mounts = {os.path.realpath(m) for m in our_mounts()}

    def add(label: str, path: Path) -> None:
        if path.exists() and os.path.realpath(path) not in mounts and not os.path.ismount(path):
            out.append(CleanItem(label, path, local_size(path)))

    add("test images", tmp / "pytest")
    for p in sorted(tmp.glob("git-rescue-*")):
        if p.is_dir():  # left behind by an interrupted git step
            add("git scratch", p)
    for p in sorted(tmp.glob("*.log")):
        add("log", p)
    for p in sorted(cfg.cache_dir.glob("sector-hashes-*.done.npy")):
        add("unfinished hashes", p)
    if hashes:
        for p in sorted(cfg.cache_dir.glob("sector-hashes-*.npy")):
            if not p.name.endswith(".done.npy"):
                add("sector hashes", p)
    return out


def untouched(cfg: Config, items: list[CleanItem]) -> list[Path]:
    """Other entries in tmp_dir (restored files, user data): listed, never removed."""
    if not cfg.tmp_dir.exists():
        return []
    taken = {i.path for i in items}
    return [p for p in sorted(cfg.tmp_dir.iterdir()) if p not in taken]


def all_mounts() -> list[str]:
    with open("/proc/self/mounts") as fh:
        return [line.split()[1].replace("\\040", " ") for line in fh if line.strip()]


def remove(items: list[CleanItem]) -> list[str]:
    """Delete the items; anything with a mount point inside is skipped (returned)."""
    mounts = [os.path.realpath(m) for m in all_mounts()]
    skipped = []
    for i in items:
        root = os.path.realpath(i.path)
        if any(m == root or m.startswith(root + os.sep) for m in mounts):
            skipped.append(f"{i.path} (something is mounted in it)")
            continue
        if i.path.is_dir() and not i.path.is_symlink():
            shutil.rmtree(i.path)
        else:
            i.path.unlink(missing_ok=True)
    return skipped
