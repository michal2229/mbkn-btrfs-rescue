# Development

```
scripts/setup.sh              # uv sync --extra fuse, config, dirs
uv run pytest                 # end-to-end tests on mkfs-built images (needs mkfs.btrfs)
uv run ruff check . && uv run ruff format --check .
```

Test images are created under `<tmp_dir>/pytest` (from the config), not `/tmp`. The suite
(~30 s) needs `mkfs.btrfs` (btrfs-progs with `--rootdir` subvolume support, 6.12+); the
real-mount test also needs `/dev/fuse` and `fusermount3` and is skipped otherwise, the git test
needs `git`.

CI (`.github/workflows/ci.yml`) runs lint and the suite in a Fedora container on every push to
`main`/`v*` branches and on pull requests.

| file | covers |
|---|---|
| `test_units.py` | extent painting vs a byte-by-byte reference, checksum-leaf index vs SQL, per-sector counting, file-type sniffing, index migration |
| `test_scan.py` | chunk size, split and interrupted/resumed scans find identical blocks |
| `test_roundtrip.py` | scan → extract → read for no/zlib/lzo/zstd compression and 4 KiB nodes, CLI commands, excludes, corrupted files, compatibility warnings |
| `test_consistency.py` | one image with every case (intact, damaged, lost, no data, newer version broken, truncated to empty, reflink/prealloc ordering, bad sector outside the referenced range, unchecksummed zeros/garbage): every file in exactly one category folder, folder counts = README totals, `all/` = everything minus lost, `best/` content, sizes = content everywhere, no empty directories, `restore` = `best/`, `review` rows and ranges, `restore --fill-older`, shell smoke test |
| `test_match.py` | bad sectors recovered from a plain copy and as zeros, a lone sector and a file without copies stay lost, `classify` matches by default and `restore` writes the patched content, chance matches (planted in the hash array) are ignored or rejected on re-read |
| `test_gitrescue.py` | lost work-tree files come back from the index (verified, incl. staged changes newer than HEAD) and HEAD (older version, "size differs"); hooks are not run |
| `test_fuse_layout.py` | mount folders through the FUSE operations: history, live refresh, flat mode, hidden unreadable files |
| `test_fuse_mount.py` | a real kernel mount: listing, reading, symlinks, read-only |

Tests only ever corrupt their own test images.

## Profiling on a real index

The index can be opened read-only next to a running mount, e.g.:

```python
con = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
fs = RescueFS(con, Device(cfg.device), exclude=cfg.exclude)
```

`cProfile` around `fs.at(None).dir_mask(Node(tree, 256))` profiles categorisation of one
subvolume; `RescueOps(fs).readdir/getattr` profiles the mount without mounting.

## Realistic test image (needs sudo)

`mkfs.btrfs --rootdir` cannot create deleted files. For a real "accident":

```
scripts/make-test-image.sh realistic --size 1G         # rm -rf + subvolume delete
scripts/make-test-image.sh realistic --discard         # same, mounted with discard (TRIM)
```

It loop-mounts an image, writes a small project in several transactions (edit, rename), keeps a
reference copy, then deletes the project and a subvolume. Recover it with the printed commands
and compare against `<tmp_dir>/images/realistic/src`.

## Layout

```
src/mbkn_btrfs_rescue/
  ondisk.py    on-disk structures and parsers (superblock, headers, items, feature flags)
  compat.py    supported features and warnings
  checksum.py  crc32c / xxhash64 / sha256 / blake2b
  device.py    read-only device access
  db.py        SQLite schema, helpers, in-place migration of older indexes
  scan.py      pass 1: device sweep (read-ahead thread)
  extract.py   pass 2: leaves -> item tables
  classify.py  pass 3: per-sector checks, categories, best versions, directory masks
  sectorhash.py  per-sector checksums of the whole device (cached .npy, worker processes)
  match.py     pass 4: bad sectors from confirmed identical copies -> sector_patch
  pipeline.py  analyze: resumable scan -> extract -> classify, status for the live mount
  model.py     RescueFS: namespace, layouts (painting), content, verification, best version
  sniff.py     does unchecksummed content fit its file type?
  restore.py   copy-out with report, --fill-older
  review.py    review list (TSV) of unverified/damaged files with byte ranges
  gitrescue.py lost work-tree files from .git objects via the git CLI
  shell.py     interactive browser
  fusefs.py    FUSE operations (mfusepy)
  cli.py       argparse entry point
```

Stored results carry versions: `MASK_VERSION` (model.py) forces re-categorisation when the
category rules change, `CHECK_VERSION` makes `classify` re-check exactly the extents whose
per-sector rules changed, and `db._migrate` adds new columns to old indexes.

Libraries used rather than re-implemented: `dissect.btrfs` (zlib/lzo/zstd extent decoding),
`crc32c`, `xxhash`, `numpy` (vectorised scan filter), `mfusepy` (FUSE).
