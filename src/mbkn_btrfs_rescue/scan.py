"""Pass 1: sweep the raw device for btrfs tree blocks and index them.

Every sector-aligned offset is tested with a vectorised (numpy) plausibility filter on the
header fields; survivors are checksum-verified. Valid blocks of *any* filesystem UUID are
recorded, so blocks of a filesystem that was overwritten by a new mkfs are found as well.
Blocks with a bad checksum are recorded only for UUIDs listed in `keep_bad_fsids`.
"""

from __future__ import annotations

import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np

from .checksum import BlockVerifier
from .db import get_meta, s64, set_meta
from .device import Device
from .ondisk import HEADER_SIZE, ITEM_SIZE, SUPER_MAGIC, first_last_keys, parse_header


@dataclass
class ScanParams:
    nodesize: int = 16384
    sectorsize: int = 4096
    csum_type: int = 0
    fsids: set[bytes] | None = None  # None = accept any UUID
    keep_bad_fsids: set[bytes] = field(default_factory=set)
    start: int = 0
    end: int | None = None
    chunk_bytes: int = 64 << 20


def _read_ahead(
    dev: Device, pos: int, end: int, p: ScanParams
) -> Iterator[tuple[int, bytearray, int]]:
    """Yield (pos, buffer, valid bytes) per chunk while a thread reads the next one.

    Three reused buffers: one being processed, one queued, one being filled - so disk reads
    overlap parsing and no 64 MiB buffer is allocated per chunk.
    """
    size = p.chunk_bytes + p.nodesize  # overlap: full node at chunk tail
    bufs = [bytearray(size) for _ in range(3)]
    q: queue.Queue = queue.Queue(maxsize=1)
    stop = threading.Event()

    def producer() -> None:
        at, i = pos, 0
        try:
            while at < end and not stop.is_set():
                buf = bufs[i % 3]
                n = dev.readinto(buf, at)
                item = (at, buf, n)
                while not stop.is_set():
                    try:
                        q.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if n <= 0:
                    return
                at += min(p.chunk_bytes, end - at)
                i += 1
        except BaseException as err:  # hand errors to the consumer
            q.put(err)
            return
        q.put(None)

    t = threading.Thread(target=producer, name="scan-read-ahead", daemon=True)
    t.start()
    try:
        while (item := q.get()) is not None:
            if isinstance(item, BaseException):
                raise item
            yield item
            if item[2] <= 0:
                return
    finally:
        stop.set()
        t.join(timeout=5)


def _candidates(buf: np.ndarray, rows: int, p: ScanParams) -> np.ndarray:
    """Indices (in sector units) of blocks whose header looks like a tree block."""
    blocks = buf[: rows * p.sectorsize].reshape(rows, p.sectorsize)
    bytenr = np.ascontiguousarray(blocks[:, 48:56]).view("<u8").ravel()
    gen = np.ascontiguousarray(blocks[:, 80:88]).view("<u8").ravel()
    nritems = np.ascontiguousarray(blocks[:, 96:100]).view("<u4").ravel()
    level = blocks[:, 100]
    max_items = (p.nodesize - HEADER_SIZE) // ITEM_SIZE
    mask = (
        (bytenr != 0)
        & (bytenr % p.sectorsize == 0)
        & (gen != 0)
        & (gen < (1 << 48))
        & (level < 8)
        & (nritems <= max_items)
    )
    if p.fsids is not None:
        fs = blocks[:, 32:48]
        m2 = np.zeros(rows, dtype=bool)
        for fsid in p.fsids | p.keep_bad_fsids:
            m2 |= (fs == np.frombuffer(fsid, dtype=np.uint8)).all(axis=1)
        mask &= m2
    return np.nonzero(mask)[0]


def scan(
    dev: Device,
    con: sqlite3.Connection,
    p: ScanParams,
    resume: bool = False,
    progress: bool = True,
    on_chunk: Callable[[int, int, int], None] | None = None,
) -> int:
    """Sweep [start, end); `on_chunk(lo, hi, end)` runs after each committed chunk."""
    verify = BlockVerifier(p.csum_type, p.nodesize)
    end = min(p.end or dev.size, dev.size)
    pos = p.start
    if resume and (saved := get_meta(con, "scan_pos")):
        pos = max(pos, int(saved))
    set_meta(con, "nodesize", p.nodesize)
    set_meta(con, "sectorsize", p.sectorsize)
    set_meta(con, "csum_type", p.csum_type)
    set_meta(con, "device_size", dev.size)
    con.commit()
    dev.advise_sequential()

    found = 0
    t0 = last = time.monotonic()
    start_pos = pos
    insert = "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    for chunk_pos, raw, got in _read_ahead(dev, pos, end, p):
        pos = chunk_pos
        want = min(p.chunk_bytes, end - pos)
        rows = min(want, got) // p.sectorsize
        if rows == 0:
            break
        arr = np.frombuffer(raw, dtype=np.uint8, count=got)
        batch = []
        for idx in _candidates(arr, rows, p):
            off = int(idx) * p.sectorsize
            block = bytes(raw[off : min(off + p.nodesize, got)])
            if len(block) < p.nodesize or block[64:72] == SUPER_MAGIC:
                continue
            h = parse_header(block)
            ok = verify(block)
            if not ok and h.fsid not in p.keep_bad_fsids:
                continue
            if p.fsids is not None and ok and h.fsid not in p.fsids:
                continue
            keys = first_last_keys(block, p.nodesize, h.level, h.nritems)
            (fo, ft, ff), (lo, lt, lf) = keys if keys else ((None,) * 3, (None,) * 3)
            batch.append(
                (
                    pos + off,
                    s64(h.bytenr),
                    h.generation,
                    s64(h.owner),
                    h.level,
                    h.nritems,
                    h.fsid,
                    int(ok),
                    None if fo is None else s64(fo),
                    ft,
                    None if ff is None else s64(ff),
                    None if lo is None else s64(lo),
                    lt,
                    None if lf is None else s64(lf),
                )
            )
        if batch:
            con.executemany(insert, batch)
            found += len(batch)
        pos += rows * p.sectorsize
        set_meta(con, "scan_pos", pos)
        con.commit()
        if on_chunk is not None:
            on_chunk(pos - rows * p.sectorsize, pos, end)
        dev.drop_cache(pos - rows * p.sectorsize, rows * p.sectorsize)
        now = time.monotonic()
        if progress and (now - last > 2 or pos >= end):
            last = now
            done = pos - start_pos
            rate = done / max(now - t0, 1e-6)
            eta = (end - pos) / rate if rate else 0
            print(
                f"\r  {pos / 2**30:8.2f} / {end / 2**30:.2f} GiB  "
                f"{rate / 2**20:7.1f} MiB/s  blocks: {found:>9}  ETA {eta / 60:5.1f} min ",
                end="",
                file=sys.stderr,
                flush=True,
            )
    if progress:
        print(file=sys.stderr)
    set_meta(con, "scan_done", int(pos >= end))
    con.commit()
    return found
