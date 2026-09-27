"""A checksum of every sector on the device, to find copies of data by its expected checksum.

The result is a flat array (one key per sector, physical order) cached as a .npy file next to
the index. Keys are the btrfs data checksum of the sector: the whole crc32c value, or the first
8 bytes of longer checksums (candidates are re-verified with the full checksum before use).
Hashing runs in worker processes over large chunks and can resume after an interruption.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .checksum import csum_function
from .device import Device
from .ondisk import CSUM_SIZES

CHUNK = 64 << 20


def key_dtype(csum_type: int) -> type:
    return np.uint32 if CSUM_SIZES[csum_type] == 4 else np.uint64


def csum_key(csum: bytes, csum_type: int) -> int:
    """The array key of a raw on-disk checksum."""
    return int.from_bytes(csum[: 4 if CSUM_SIZES[csum_type] == 4 else 8], "little")


def zero_key(sectorsize: int, csum_type: int) -> int:
    return csum_key(csum_function(csum_type)(bytes(sectorsize)), csum_type)


def _hash_chunk(path: str, offset: int, length: int, sectorsize: int, csum_type: int):
    fn = csum_function(csum_type)
    dev = Device(path)
    try:
        data = dev.pread(length, offset)
        dev.drop_cache(offset, length)  # read once: do not push everything else out of RAM
    finally:
        dev.close()
    n = len(data) // sectorsize
    mv = memoryview(data)
    width = 4 if CSUM_SIZES[csum_type] == 4 else 8
    raw = b"".join(fn(mv[i * sectorsize : (i + 1) * sectorsize])[:width] for i in range(n))
    return offset, np.frombuffer(raw, dtype=key_dtype(csum_type))


class SectorHashes:
    """Per-sector keys of a device, built once and cached in `path`."""

    def __init__(self, path: Path, dev: Device, sectorsize: int, csum_type: int):
        self.path = path
        self.dev = dev
        self.sectorsize = sectorsize
        self.csum_type = csum_type
        self.nsect = dev.size // sectorsize
        self._done_path = path.with_suffix(".done.npy")

    @property
    def complete(self) -> bool:
        return self.path.exists() and not self._done_path.exists()

    def build(
        self,
        workers: int | None = None,
        progress: bool = True,
        report: Callable[[str], None] | None = None,
    ) -> None:
        if self.complete:
            return
        nchunks = -(-self.nsect * self.sectorsize // CHUNK)
        dtype = key_dtype(self.csum_type)
        keys: np.memmap | None = None
        done: np.ndarray | None = None
        if self.path.exists() and self._done_path.exists():
            keys = np.lib.format.open_memmap(self.path, mode="r+")
            done = np.load(self._done_path)
            if keys.shape != (self.nsect,) or keys.dtype != dtype or done.shape != (nchunks,):
                keys, done = None, None
        if keys is None or done is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            keys = np.lib.format.open_memmap(self.path, mode="w+", dtype=dtype, shape=(self.nsect,))
            done = np.zeros(nchunks, dtype=bool)
            np.save(self._done_path, done)
        todo = [int(c) for c in np.flatnonzero(~done)]
        limit = self.nsect * self.sectorsize
        t0 = last = time.monotonic()
        workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
        with ProcessPoolExecutor(workers) as pool:
            futs = [
                pool.submit(
                    _hash_chunk,
                    self.dev.path,
                    c * CHUNK,
                    min(CHUNK, limit - c * CHUNK),
                    self.sectorsize,
                    self.csum_type,
                )
                for c in todo
            ]
            for i, fut in enumerate(futs, 1):
                offset, arr = fut.result()
                s = offset // self.sectorsize
                keys[s : s + len(arr)] = arr
                done[offset // CHUNK] = True
                now = time.monotonic()
                if now - last > 5 or i == len(futs):
                    keys.flush()
                    np.save(self._done_path, done)
                    last = now
                    rate = i * CHUNK / max(now - t0, 1e-6)
                    eta = (len(futs) - i) * CHUNK / rate
                    msg = (
                        f"hashing sectors {i}/{len(futs)} chunks"
                        f"  {rate / 2**20:.0f} MiB/s  ETA {eta / 60:.1f} min"
                    )
                    if progress:
                        print(f"\r  {msg} ", end="", file=sys.stderr, flush=True)
                    if report:
                        report(msg)
        keys.flush()
        del keys
        self._done_path.unlink()
        if progress and todo:
            print(file=sys.stderr)

    def load(self) -> np.ndarray:
        """The key array (memory-mapped, read-only)."""
        if not self.complete:
            raise RuntimeError("sector hashes are not built yet")
        return np.load(self.path, mmap_mode="r")


def hashes_path(cache_dir: Path, fsid_hex: str, dev: Device, sectorsize: int) -> Path:
    return cache_dir / f"sector-hashes-{fsid_hex[:12]}-{dev.size}-{sectorsize}.npy"
