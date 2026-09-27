# Development

```
scripts/setup.sh              # uv sync --extra fuse, config, dirs
uv run pytest                 # end-to-end tests on mkfs-built images (needs mkfs.btrfs)
scripts/checks.sh             # ruff, ruff format, mypy, shellcheck, codespell, gitleaks
```

The test suite keeps the images of the last two runs under `<tmp_dir>/pytest` and removes
older ones; `mbkn-btrfs-rescue clean` removes the rest.

To push with a specific SSH key without an agent, set it for this clone only (stored in
`.git/config`, never committed):

```
git config core.sshCommand "ssh -i ~/.ssh/<key> -o IdentitiesOnly=yes"
```

`scripts/setup.sh` enables the git hooks in `scripts/git-hooks`: `scripts/checks.sh` before
every commit (a few seconds), the full test suite before every push. The secret scan runs
locally when `gitleaks` is installed; CI always runs it over the whole history.

Test images are created under `<tmp_dir>/pytest` (from the config), not `/tmp`. The suite
(~30 s) needs `mkfs.btrfs` (btrfs-progs with `--rootdir` subvolume support, 6.12+); the
real-mount test also needs `/dev/fuse` and `fusermount3` and is skipped otherwise, the git test
needs `git`.

CI (`.github/workflows/ci.yml`) runs the same checks, a gitleaks scan of the whole history,
the suite and a package build (`uv build`) in a Fedora container on every push to `main`/`v*`
branches and on pull requests.

| file | covers |
|---|---|
| `test_units.py` | extent painting vs a byte-by-byte reference, checksum-leaf index vs SQL, per-sector counting, file-type sniffing, index migration, range helpers |
| `test_scan.py` | chunk size, split and interrupted/resumed scans find identical blocks |
| `test_roundtrip.py` | scan → extract → read for no/zlib/lzo/zstd compression and 4 KiB nodes, CLI commands, excludes, corrupted files, compatibility warnings |
| `test_consistency.py` | one image with every case (intact, damaged, lost, no data, newer version broken, truncated to empty, reflink/prealloc ordering, bad sector outside the referenced range, unchecksummed zeros/garbage): every file in exactly one category folder, folder counts = README totals, `all/` = everything minus lost, `best/` content, sizes = content everywhere, no empty directories, `restore` = `best/`, `review` rows and ranges, a damaged file filled from an older version in `best/`, `patched/`, `PATCHED.tsv` and `restore`, shell smoke test, shell `gen`/`exclude` refresh listings |
| `test_match.py` | bad sectors recovered from a plain copy and as zeros, a lone sector and a file without copies stay lost, `classify` matches by default and `restore` writes the patched content, chance matches (planted in the hash array) are ignored or rejected on re-read, DUP data recovered from its second copy, identical compressed files matched |
| `test_gitrescue.py` | lost work-tree files come back from the index (verified, incl. staged changes newer than HEAD) and HEAD (older version, "size differs"), untracked files are reported; hooks are not run; `best/`, `patched/` and `restore` serve the git content by default |
| `test_fuse_layout.py` | mount folders through the FUSE operations: history, live refresh, flat mode, hidden unreadable files, `current/` = the source tree with subvolumes in place and without deleted names, a nested subvolume mounted in place (listed once in merged views), the index fallback without a superblock gives the same names |
| `test_fuse_mount.py` | a real kernel mount: listing, reading, symlinks, read-only; `status` sees it, sizes never read through it, `umount` |
| `test_tools.py` | only the tool's own processes match (not an editor on the project directory), `clean` removes only tool-made files, sizes do not follow symlinks |

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
  restore.py   copy-out with report (reconstructions as in best/)
  patching.py  reconstructions for damaged/lost files (older versions, git)
  current.py   the newest trees of all subvolumes: names that exist now
  tools.py     status / umount / stop / clean
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
