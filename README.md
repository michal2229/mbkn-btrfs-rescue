# mbkn-btrfs-rescue

Read-only recovery of deleted or lost files from a **btrfs** filesystem, when mounting,
`btrfs restore` and other tools no longer see them.

It sweeps the raw device for every btrfs tree block that still exists (all generations, even
of a filesystem overwritten by a new `mkfs`), indexes them in SQLite, rebuilds the directory
tree bottom-up from the leaves, and lets you:

* **browse** it in an interactive shell (`ls`, `cd`, `find`, `grep`, `stat`, `versions`, `cat`),
* **mount** it read-only via FUSE, grouped by category, with history per generation, live
  while the analysis is still running,
* **classify** every file as intact / unverified / damaged / lost by checking btrfs data
  checksums sector by sector (and, where no checksum survives, whether the data is plausible),
* **recover bad sectors from identical copies** elsewhere on the disk (plain copies of files,
  other subvolumes, DUP mirrors, old locations), found by their expected checksums,
* get the **most complete version** of every file automatically (`best/` in the mount, default
  for `restore`): the newest good one, an older one when the newest broke, and for damaged or
  lost files a reconstruction - bad ranges filled from older versions, or the file's content
  from its git repository - listed separately in `patched/` and `PATCHED.tsv`,
* bring back **lost source files from git**: staged, stashed or committed content of lost
  work-tree files, verified against the lost file's checksums (automatic in `best/`;
  `git-rescue` for a report),
* see the disk **as a normal mount would show it now** (`current/`),
* **review** what needs a human look: unverified and damaged files with their bad byte ranges,
* **restore** only what is salvageable (by default), with a report,
* go back to **any older version** of files (`history/gen-N/`, `--at-gen`),
* skip noise like `.venv` / `.venv-tools` (configurable).

The device is only ever opened `O_RDONLY`; a helper script also freezes it at the block layer.

## Quick start

```bash
git clone git@github.com:michal2229/mbkn-btrfs-rescue.git
cd mbkn-btrfs-rescue
scripts/setup.sh --device /dev/mapper/luks-XXXX      # uv env + local config + dirs
scripts/device-access.sh grant                       # sudo: freeze read-only + read ACL

uv run mbkn-btrfs-rescue mount ~/rescue              # mounts at once, analyses in background
uv run mbkn-btrfs-rescue status                      # (another terminal) progress, what runs
uv run mbkn-btrfs-rescue stop                        # when done: unmount, pause analyses
```

Open `~/rescue` in any file manager:

```
~/rescue/
  README.txt          what everything means + live analysis progress
  PATCHED.tsv         where each reconstructed file in best/ came from
  best/               the most complete version of every file            <- start here
  patched/            the files of best/ that are reconstructed (check these)
  intact/             content verified by btrfs checksums
  unverified/         no checksum on record, probably fine
  damaged/            partially readable
  lost/               names and metadata only, content gone
  all/                everything readable, newest version of each file
  history/gen-N/      everything as it was up to generation N
  current/            the disk as a normal mount shows it now
```

Each folder keeps the original paths (`<subvolume>@<id>/path/to/file`). The mount refreshes
itself while the analysis runs; category folders fill in when classification finishes.

Without FUSE, or step by step:

```bash
uv run mbkn-btrfs-rescue analyze                     # scan + extract + classify + match, resumable
uv run mbkn-btrfs-rescue shell                       # ls / cd / find / grep / stat / restore
uv run mbkn-btrfs-rescue restore /_work@260/proj ~/recovered   # best versions, good files only
uv run mbkn-btrfs-rescue git-rescue /_work@260 --dest ~/recovered-git   # lost files from .git
uv run mbkn-btrfs-rescue review -o ~/review.tsv      # what to check by hand
uv run mbkn-btrfs-rescue clean                       # remove test images, scratch, logs
```

### Getting the most back

Everything below runs by default during the analysis; `best/` (and `restore`) serve the result.
For each file, `best/` has the first of these that exists:

1. verified content: the newest version that is intact (or unverified but plausible), else an
   older one; bad sectors already replaced by identical copies found on the disk,
2. a git blob byte-identical to the lost file (index, stash or HEAD),
3. the best damaged version with all bad ranges filled from older versions,
4. a git blob of the same size, then 5. an older git blob of another size,
6. the best damaged version, partly filled or as it is.

Cases 2-6 are also in `patched/` and `PATCHED.tsv` (with their source): check those first.
`review` lists unverified and damaged files with the exact bad byte ranges; `lost/` shows what
existed (names, sizes, dates), to know what to recreate.

### Copy from `best/` or use `restore`?

Both give the same bytes: `restore` writes exactly the versions `best/` shows. Copying from
`best/` with a file manager or `cp -a` is fine for picking files by hand. `restore` adds:

* only intact, unverified and completely reconstructed files by default (`best/` also has
  damaged ones),
* a TSV report of every file with category, sector counts and which version was used,
* no FUSE needed, never overwrites existing files unless `--overwrite`,
* speed: reads straight from the device, without the FUSE round trips.

`cp -a ~/rescue/intact/...` is the equivalent of `restore` for files whose newest version is
fine; `best/` additionally has the rescued older versions.

## Requirements

* Linux, Python ≥ 3.14, [uv](https://docs.astral.sh/uv/)
* for FUSE: `fuse3` (`fusermount3`); for reconstructions from git repositories: `git`
* for tests: `btrfs-progs` 6.12+ (`mkfs.btrfs --rootdir` with subvolumes)
* read access to the device (see `scripts/device-access.sh`)

## Compatibility

Works on the btrfs **on-disk format**, not through the kernel, so the kernel and btrfs-progs
versions that created the filesystem do not matter; its **feature flags** do. `info` lists
them, and every command (and `README.txt` in the mount) warns when something is unsupported.

| | supported | not supported |
|---|---|---|
| layout | single device; `single`, `DUP` profiles; one copy of `RAID1`/`RAID1C3`/`RAID1C4` on the given device | `RAID0`, `RAID10`, `RAID5/6`, `raid-stripe-tree`, `extent-tree-v2` |
| checksums | crc32c, xxhash64, sha256, blake2b | — |
| compression | none, zlib, lzo, zstd | — |
| features | `no-holes`, `skinny-metadata`, `extended-iref`, `metadata_uuid`, free-space-tree, `block-group-tree`, quotas, verity | fscrypt content (listed, not decrypted); zoned and mixed block groups untested |
| geometry | sector size 4 KiB, any node size (4–64 KiB) | other sector sizes untested |

This covers filesystems created by anything from Linux 3.x to current kernels (tested with
kernel 7.2 and btrfs-progs 7.1). Details: [docs/compatibility.md](docs/compatibility.md).

## Documentation

* [docs/usage.md](docs/usage.md) — every command, the shell, mount, restore
* [docs/how-it-works.md](docs/how-it-works.md) — scan/extract design, verification statuses, limits
* [docs/compatibility.md](docs/compatibility.md) — btrfs features, profiles, warnings
* [docs/configuration.md](docs/configuration.md) — config file and paths
* [docs/development.md](docs/development.md) — tests, realistic test images, code layout

## Before you start — protect your data

1. Stop writing to the filesystem. Unmount it. Don't run `btrfs check --repair`.
2. Every write (including TRIM/discard) can destroy what is still recoverable.
3. Restore to a **different** disk.

## License

GPL-3.0 — see [LICENSE](LICENSE).
