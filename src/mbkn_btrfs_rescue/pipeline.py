"""Full, resumable analysis: scan (with incremental extract) -> classify.

Progress is written to meta `analyze_status` so a live mount can show it in README.txt.
Every stage commits frequently; re-running continues where it stopped.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .classify import categorise, classify_extents
from .compat import device_warnings
from .db import get_meta, set_meta
from .device import Device
from .extract import STAT_KEYS, extract_leaves, fsid_summary, prepare_extract
from .model import CHECK_VERSION, RescueFS, classify_key, patch_serials
from .ondisk import Superblock
from .scan import ScanParams, scan

STATE_KEY, STATUS_KEY, COMPAT_KEY = "analyze_state", "analyze_status", "compat_warnings"
STATES = ("scanning", "classifying", "complete")


def best_superblock(dev: Device) -> Superblock | None:
    valid = [sb for sb in dev.superblocks() if sb.valid_magic]
    return max(valid, key=lambda s: s.generation) if valid else None


def analysis_state(con: sqlite3.Connection) -> str | None:
    return get_meta(con, STATE_KEY)


def classification_outdated(con: sqlite3.Connection, exclude: list[str]) -> bool:
    """True when stored checks/categories were made by older rules or another exclude list."""
    return int(get_meta(con, "check_version", "1")) < CHECK_VERSION or get_meta(
        con, "classify_exclude"
    ) != classify_key(exclude, patch_serials(con))


def _status(con: sqlite3.Connection, text: str, echo: bool) -> None:
    set_meta(con, STATUS_KEY, f"{time.strftime('%H:%M:%S')} {text}")
    con.commit()
    if echo:
        print(f"\r  {text} ", end="", file=sys.stderr, flush=True)


def prepare(
    dev: Device,
    con: sqlite3.Connection,
    *,
    fsid: bytes | None = None,
    params: ScanParams | None = None,
    restart: bool = False,
) -> ScanParams:
    """Fast, synchronous part: choose filesystem and parameters, reset state if needed."""
    sb = best_superblock(dev)
    if params is None:
        params = ScanParams(
            nodesize=sb.nodesize if sb else 16384,
            sectorsize=sb.sectorsize if sb else 4096,
            csum_type=sb.csum_type if sb else 0,
        )
    params.keep_bad_fsids = {s.header_fsid for s in dev.superblocks() if s.valid_magic}
    target = fsid or (sb.header_fsid if sb else None)
    if restart or get_meta(con, "scan_pos") is None:
        con.execute("DELETE FROM nodes")
        con.execute("DELETE FROM meta")
        con.commit()
    if target and get_meta(con, "fsid") != target.hex():
        prepare_extract(con, target, dev.superblocks())
    set_meta(con, STATE_KEY, get_meta(con, STATE_KEY) or "scanning")
    set_meta(con, COMPAT_KEY, json.dumps(device_warnings(dev.superblocks())))
    con.commit()
    return params


def analyze(
    dev: Device,
    con: sqlite3.Connection,
    params: ScanParams,
    *,
    exclude: list[str],
    quick: bool = False,
    echo: bool = True,
    stop: Callable[[], bool] | None = None,
    match_dir: Path | None = None,
    scratch: Path | None = None,
    patch: bool = True,
) -> str:
    """Run the remaining stages. Returns the final state."""
    state = get_meta(con, STATE_KEY) or "scanning"
    if state == "scanning":
        fsid_hex = get_meta(con, "fsid")
        stats = dict.fromkeys(STAT_KEYS, 0)

        def on_chunk(lo: int, hi: int, end: int) -> None:
            if fsid_hex:
                extract_leaves(
                    dev,
                    con,
                    bytes.fromhex(fsid_hex),
                    params.nodesize,
                    phys_range=(lo, hi),
                    stats=stats,
                    progress=False,
                )
            _status(
                con,
                f"scanning {hi / 2**30:.1f}/{end / 2**30:.1f} GiB, {stats['inodes']} inode records",
                echo,
            )
            if stop and stop():
                raise KeyboardInterrupt

        if get_meta(con, "scan_done") != "1":
            scan(dev, con, params, resume=True, progress=False, on_chunk=on_chunk)
        if not fsid_hex:  # no superblock: pick the dominant filesystem now, extract all
            rows = fsid_summary(con)
            if not rows:
                raise SystemExit("no btrfs tree blocks found on the device")
            prepare_extract(con, rows[0][0], dev.superblocks())
            extract_leaves(dev, con, rows[0][0], params.nodesize, progress=echo)
        set_meta(con, STATE_KEY, "classifying")
        con.commit()
        state = "classifying"
    if state == "classifying":
        fs = RescueFS(con, dev, exclude=exclude)
        _status(con, "classifying: checking data extents", echo)
        classify_extents(fs, quick=quick, progress=echo, report=lambda m: _status(con, m, False))
        if match_dir is not None and not quick:
            from .match import run_match

            _status(con, "classifying: recovering bad sectors from copies", echo)
            run_match(fs, match_dir, progress=echo, report=lambda m: _status(con, m, False))
        _status(con, "classifying: categorising files, reconstructing damaged/lost ones", echo)
        categorise(fs, scratch, patch=patch and not quick, progress=echo)
        set_meta(con, STATE_KEY, "complete")
        con.commit()
        state = "complete"
    _status(con, "analysis complete", echo)
    if echo:
        print(file=sys.stderr)
    return state
