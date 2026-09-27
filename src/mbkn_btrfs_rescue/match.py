"""Pass 4: recover bad sectors from identical copies found elsewhere on the device.

A sector that fails verification still has its expected checksum in the checksum tree. The same
data often exists somewhere else: plain (non-reflink) copies of files, the second copy of DUP
data, another subvolume, an old physical location left behind by a balance. Every sector of the
device is hashed once (`sectorhash`), and the expected checksums of bad sectors are looked up
among those hashes.

crc32c has only 32 bits: on a 500 GB device a random sector matches a given checksum by chance
with a probability of a few percent (on a real disk: 1.9 million chance matches for 67 million
bad sectors). A candidate is therefore used only when it is *confirmed*: the logical neighbour
of the bad sector (in the same or an adjacent extent, bad or good) matches the device sector
right next to the candidate, so the copy has the same layout. A chance match passes this with
a probability of about 2^-32. All-zero sectors never confirm anything, and unconfirmed matches
are ignored - a lone sector (a small file) cannot be recovered this way. With 64-bit or longer
checksums every match counts. Every chosen sector is re-read and checked against the full
expected checksum before it is recorded.

A bad sector whose expected checksum is that of an all-zero sector held zeros: it is recovered
without reading anything.

Results go to the `sector_patch` table, one row per extent: for every sector, where to read it
from instead. Reading and categories apply the patches (see RescueFS.check_extent/read).
"""

from __future__ import annotations

import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .db import set_meta
from .model import PATCH_COPY, PATCH_ZERO, RescueFS
from .ondisk import CSUM_SIZES
from .sectorhash import SectorHashes, csum_key, hashes_path, zero_key

MAX_CANDIDATES = 8  # device sectors tried per expected checksum (common content repeats a lot)
BATCH_SECTORS = 4_000_000


def _progress(msg: str) -> None:
    print(f"\r  {msg} ", end="", file=sys.stderr, flush=True)


def _targets(fs: RescueFS) -> list[tuple[int, int, int]]:
    """Extents with bad sectors or without a known location, in logical order."""
    return [
        (b, n, g)
        for b, n, g in fs.con.execute(
            "SELECT disk_bytenr, disk_len, egen FROM extent_status "
            "WHERE bad > 0 OR mapped = 0 ORDER BY disk_bytenr"
        )
    ]


def _bad_mask(fs: RescueFS, key: tuple[int, int, int], nsect: int) -> np.ndarray:
    c = fs.raw_extent_status(key)
    if not c.mapped:
        return np.ones(nsect, dtype=bool)
    if c.sectors is not None:
        return np.frombuffer(zlib.decompress(c.sectors), dtype=np.uint8) == 1
    return np.full(nsect, bool(c.bad) and not c.good and not c.nocsum)


def match_copies(fs: RescueFS, hashes: np.ndarray, progress: bool = True) -> dict[str, int]:
    """Find copies for all bad sectors and store them in `sector_patch`. Returns counts."""
    ss, ct = fs.sectorsize, fs.csum_type
    strong_csum = CSUM_SIZES[ct] > 4
    zkey = zero_key(ss, ct)
    if progress:
        _progress("matching: sorting device sector hashes")
    order = np.argsort(hashes, kind="stable").astype(np.int64)
    dsorted = np.asarray(hashes)[order]
    targets = _targets(fs)
    stats = dict.fromkeys(
        ("extents", "bad", "no_csum", "zero", "copied", "unconfirmed", "stale"), 0
    )
    stats["extents"] = len(targets)
    rows: list[tuple] = []
    t0 = last = time.monotonic()

    i = 0
    while i < len(targets):
        # --- one batch of extents: expected keys of every sector
        batch: list[tuple] = []
        keys_l: list[np.ndarray] = []
        ext_l: list[np.ndarray] = []
        idx_l: list[np.ndarray] = []
        bad_l: list[np.ndarray] = []
        sums: list[list[bytes | None]] = []
        size = 0
        while i < len(targets) and (size < BATCH_SECTORS or not batch):
            key = targets[i]
            i += 1
            b, n, g = key
            nsect = max(1, -(-n // ss))
            expected = fs._data_csums(b, nsect, g)
            bad = _bad_mask(fs, key, nsect)
            have = np.array([s is not None for s in expected], dtype=bool)
            k = np.array([csum_key(s, ct) if s else 0 for s in expected], dtype=np.uint64)
            j = len(batch)
            batch.append((key, nsect, bad, k, have))
            sums.append(expected)
            stats["bad"] += int(bad.sum())
            stats["no_csum"] += int((bad & ~have).sum())
            probe = have & (k != zkey)
            keys_l.append(k[probe])
            ext_l.append(np.full(int(probe.sum()), j, dtype=np.int64))
            idx_l.append(np.flatnonzero(probe).astype(np.int64))
            bad_l.append(bad[probe])
            size += nsect
        starts = np.array([k[0][0] // ss for k in batch], dtype=np.int64)
        pk = np.concatenate(keys_l).astype(dsorted.dtype)
        pe, pi, pb = np.concatenate(ext_l), np.concatenate(idx_l), np.concatenate(bad_l)

        # --- candidates: device sectors with the same key (joined, capped per probe)
        lo = np.searchsorted(dsorted, pk, "left")
        cnt = np.minimum(np.searchsorted(dsorted, pk, "right") - lo, MAX_CANDIDATES)
        sel = np.flatnonzero(cnt)
        c = cnt[sel]
        rep = np.repeat(sel, c)
        within = np.arange(len(rep)) - np.repeat(np.cumsum(c) - c, c)
        pos = order[np.repeat(lo[sel], c) + within]
        ext, idx, isbad = pe[rep], pi[rep], pb[rep]
        # distance between copy and original, in sectors: along a real copy it is constant
        lsec = starts[ext] + idx
        delta = pos - lsec
        # confirmed: the logical neighbour (lsec +- 1) matches at the same distance. Sorted by
        # (distance, logical sector) and de-duplicated (extents of several generations cover
        # the same logical sector), neighbours are adjacent in the order.
        o = np.lexsort((lsec, delta))
        dd, ll = delta[o], lsec[o]
        new = np.ones(len(o), dtype=bool)
        new[1:] = (dd[1:] != dd[:-1]) | (ll[1:] != ll[:-1])
        group = np.cumsum(new) - 1
        ud, ul = dd[new], ll[new]
        adj = np.zeros(len(ud), dtype=bool)
        step = (ud[1:] == ud[:-1]) & (ul[1:] - ul[:-1] == 1)
        adj[1:] |= step
        adj[:-1] |= step
        confirmed = np.empty(len(o), dtype=bool)
        confirmed[o] = adj[group]
        if strong_csum:
            confirmed[:] = True
        r = np.flatnonzero(isbad)
        stats["unconfirmed"] += len(np.unique(ext[r] * (1 << 32) + idx[r]))
        r = r[confirmed[r]]
        r = r[np.lexsort((idx[r], ext[r]))]
        first = np.ones(len(r), dtype=bool)
        first[1:] = (ext[r][1:] != ext[r][:-1]) | (idx[r][1:] != idx[r][:-1])
        r = r[first]
        stats["unconfirmed"] -= len(r)
        choice = {(int(ext[x]), int(idx[x])): int(pos[x]) for x in r}

        # --- verify the chosen sectors (full checksum) in physical order, runs coalesced
        want = sorted((p, e, s) for (e, s), p in choice.items())
        ok: set[tuple[int, int]] = set()
        run_start = 0
        while run_start < len(want):
            run_end = run_start + 1
            while run_end < len(want) and want[run_end][0] == want[run_end - 1][0] + 1:
                run_end += 1
            p0 = want[run_start][0]
            data = fs.dev.pread((run_end - run_start) * ss, p0 * ss)
            for p, e, s in want[run_start:run_end]:
                off = (p - p0) * ss
                if fs._csum(data[off : off + ss]) == sums[e][s]:
                    ok.add((e, s))
                else:
                    stats["stale"] += 1
            run_start = run_end

        # --- per extent patch arrays
        for j, (key, nsect, bad, k, have) in enumerate(batch):
            phys = np.full(nsect, -1, dtype=np.int64)
            kind = np.zeros(nsect, dtype=np.uint8)
            zeros = bad & have & (k == zkey)
            kind[zeros] = PATCH_ZERO
            for s in np.flatnonzero(bad & have & ~zeros).tolist():
                if (j, s) in ok:
                    phys[s] = choice[(j, s)] * ss
                    kind[s] = PATCH_COPY
            nz, ns = int((kind == PATCH_ZERO).sum()), int((kind == PATCH_COPY).sum())
            if nz + ns:
                stats["zero"] += nz
                stats["copied"] += ns
                blob = zlib.compress(phys.tobytes() + kind.tobytes(), 1)
                rows.append((*key, ns, nz, blob))

        now = time.monotonic()
        if progress and (now - last > 2 or i == len(targets)):
            last = now
            _progress(
                f"matching: extents {i}/{len(targets)}, sectors recovered: "
                f"{stats['copied']} from copies, {stats['zero']} zeros"
                f"  ({now - t0:.0f}s)"
            )
    if progress:
        print(file=sys.stderr)
    con = fs.con
    con.execute("DELETE FROM sector_patch")
    con.executemany("INSERT INTO sector_patch VALUES (?,?,?,?,?,?)", rows)
    set_meta(con, "patch_serial", f"{int(time.time())}-{len(rows)}")
    con.commit()
    fs.reset_patches()
    return stats


def run_match(
    fs: RescueFS,
    cache_dir: Path,
    progress: bool = True,
    report: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Hash the device (once, cached in `cache_dir`) and match bad sectors against it."""
    hashes = SectorHashes(
        hashes_path(cache_dir, fs.fsid.hex(), fs.dev, fs.sectorsize),
        fs.dev,
        fs.sectorsize,
        fs.csum_type,
    )
    hashes.build(progress=progress, report=report)
    if report:
        report("matching bad sectors against device hashes")
    return match_copies(fs, hashes.load(), progress)
