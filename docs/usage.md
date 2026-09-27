# Usage

All commands accept the global options `-c/--config`, `-d/--device` and `--db` **before** the
command name. Defaults come from the config file (see [configuration.md](configuration.md)).

```
uv run mbkn-btrfs-rescue [-c CONFIG] [-d DEVICE] [--db INDEX] COMMAND ...
```

## 0. Protect the device

Stop using the filesystem. Do **not** mount it (not even read-only — the kernel may replay a
log tree), do not run `btrfs check --repair`, `btrfs rescue`, or any tool that writes.

```
scripts/device-access.sh grant /dev/mapper/luks-...   # blockdev --setro + read ACL for you
scripts/device-access.sh status
```

The ACL and read-only flag vanish on reboot or when the LUKS mapping is closed.

## 1. Look at the superblocks

```
uv run mbkn-btrfs-rescue info
```

Shows label, fsid, generation, usage and the four backup roots. A very low generation
(single digits) on a disk that held data means it was re-created with `mkfs`.

## 2. Scan (pass 1)

```
uv run mbkn-btrfs-rescue scan                 # whole device
uv run mbkn-btrfs-rescue scan --resume        # continue after Ctrl-C
uv run mbkn-btrfs-rescue scan --start 100G --end 200G --force   # partial / fresh
```

Reads ahead in a background thread while parsing, so it runs close to disk speed (about
1.2 GiB/s on an NVMe behind LUKS that reads 1.6 GB/s raw; ~13 min for 500 GB at the old rate,
~7 now). At the end, and at any time via `fsids`, it prints the filesystem UUIDs found with the
number of valid blocks and generation ranges.

```
uv run mbkn-btrfs-rescue fsids
uv run mbkn-btrfs-rescue trees     # blocks per tree id; subvolume names after extract
```

## 3. Extract (pass 2)

```
uv run mbkn-btrfs-rescue extract                       # fsid with most blocks, all fs trees
uv run mbkn-btrfs-rescue extract --fsid <uuid>         # an older (overwritten) filesystem
uv run mbkn-btrfs-rescue extract --trees 5,257         # only some subvolumes (faster)
uv run mbkn-btrfs-rescue extract --allow-bad-csum      # desperate mode
uv run mbkn-btrfs-rescue subvols
```

## 3b. Classify (pass 3)

```
uv run mbkn-btrfs-rescue classify              # verify every data extent, categorise files
uv run mbkn-btrfs-rescue classify --quick      # sample 3 sectors per extent (fast estimate)
```

Reads every data extent once, in physical order (disk speed), compares every sector with the
checksum tree, and stores the result per sector. Files are then categorised by the sectors
they actually use, and the best version of each damaged/lost file is looked up (see
[how-it-works.md](how-it-works.md#categories)). Resumable; running it again only re-checks
what is new. After upgrading the tool, `classify` re-checks exactly the extents whose rules
changed and migrates the index.

After checking, `classify` also runs `match` (below; `--no-match` to skip), builds the
current state for `current/`, categorises every file and chooses reconstructions for damaged
and lost files (`--no-patch` to skip; see [best/](#fuse-mount-recommended)). On a 500 GB disk
the steps after checking take about 5 minutes; the first `match` adds the one-time hashing.

## 3c. Match: bad sectors from identical copies (pass 4)

```
uv run mbkn-btrfs-rescue match
```

Hashes every sector of the device once (about 1.3 GiB/s, cached as
`<cache_dir>/sector-hashes-*.npy`, 4 bytes per sector: 500 MB for a 500 GB disk; resumable)
and looks up the expected checksum of every bad sector among them. A copy is used only when it
is confirmed by its neighbour (see [how-it-works.md](how-it-works.md#copies)); sectors whose
expected checksum is that of zeros come back as zeros. Then categories are recomputed. Runs
automatically in `classify`, `analyze` and `mount`; takes a few minutes on a 500 GB disk.

**Shortcut:** `uv run mbkn-btrfs-rescue analyze` runs everything - scan → extract → classify
(with match, current state, reconstructions) - resumable, extracting leaves incrementally
while scanning. `--no-match` and `--no-patch` skip those steps.

## 4. Browse

### Shell

```
uv run mbkn-btrfs-rescue shell            # or: shell /_work@257/myproject
```

| command | |
|---|---|
| `ls [-l] [-a] [PATH]` | list with category markers `✓ ? ! ✗`; `-a` also shows lost and stale entries |
| `summary [PATH]` | files and bytes per category below PATH |
| `cd PATH`, `pwd` | navigate (tab completion works) |
| `tree [PATH] [DEPTH]` | recursive listing |
| `find GLOB [PATH]` | e.g. `find '*.py' /_work@257` |
| `grep REGEX [PATH]` | search file contents (files < 4 MiB) |
| `stat PATH` | inode, extents, compression, per-extent verification, category, best version |
| `versions PATH` | generations this inode was seen in |
| `gen N` / `gen off` | view everything as of generation N (older versions) |
| `cat PATH`, `less PATH` | show content |
| `restore PATH DEST [--damaged] [--lost] [--latest] [--overwrite]` | copy the best version of intact + unverified files (plus opted-in categories) to DEST |
| `exclude [NAME]`, `exclude -NAME` | show / add / remove hidden names |

### FUSE mount (recommended)

```
uv run mbkn-btrfs-rescue mount ~/rescue          # Ctrl-C, or `mbkn-btrfs-rescue umount`
```

If the index is incomplete, `mount` starts the full analysis (`analyze`) in the background
and the mounted tree refreshes itself (default every 30 s, `--refresh`); progress is shown in
`README.txt` at the mount root. The analysis log goes to `<tmp_dir>/analyze.log`.
Unmounting pauses the analysis; mounting again resumes it. When the stored categories were
made by an older version of the tool or with another exclude list (`-x`, `--no-exclude`),
`mount` re-runs classification in the background the same way (a few minutes on 500 GB; the
folders keep working meanwhile).

```
~/rescue/
  README.txt                              categories explained, counts, live progress
  PATCHED.tsv                             source of every reconstructed file in best/
  best/<subvol>@<id>/...                  most complete version of each file  <- start here
  patched/<subvol>@<id>/...               the files of best/ that are reconstructed
  intact/<subvol>@<id>/...                verified content (or inline in metadata)
  unverified/<subvol>@<id>/...            no checksum on record - probably fine, check
  damaged/<subvol>@<id>/...               some sectors bad - partially readable
  lost/<subvol>@<id>/...                  content gone - names, sizes, dates only
  all/<subvol>@<id>/...                   everything readable, newest version of each file
  history/gen-0001100/<subvol>@<id>/...   everything readable as seen up to generation 1100
  current/...                             the disk as mounted now (top level, subvolumes in place)
```

`best/` shows each file's newest version, except when an older version (up to 64 back) is
strictly better: a better category (intact > unverified > damaged > lost), fewer bad sectors
among damaged versions, or real content when the newest version is empty (files truncated when
disaster struck). Then it serves the newest such older version, with that version's size and
timestamps. `README.txt` says how many files use an older version; `stat` in the shell shows it
per file.

For files whose best version is still damaged or lost, `best/` serves a **reconstruction**
when there is one: bad ranges filled with good data of older versions, or the file's content
from its git repository (index, stash, HEAD - see section 7), in the order given in the README.
These files are also listed in `patched/` (same paths) and in `PATCHED.tsv` at the mount root
(path, source, complete, detail). Only files with nothing readable anywhere are missing from
`best/`. `classify --no-patch` turns reconstructions off.

`current/` shows the filesystem as a normal mount of the device would now: the top-level
subvolume as root, nested subvolumes where they are mounted, only names that exist in the
newest trees (no deleted files, no old names), newest content - unreadable files included, as
the kernel would show them. It is built from the latest root items (see
[how-it-works.md](how-it-works.md#current-state)).

Nested subvolumes appear once, at the top level (`<name>@<id>`), not also inside their parent
directory - otherwise every file in them would show up twice.

`all/` and `history/` leave out files without recoverable data (zeros, or lost once
classified); they stay in `lost/`. `--show-unreadable` lists them anyway.

Category folders keep the original directory structure and show only directories that contain
files of that category. They are empty until classification has finished.

A generation view contains everything *seen* up to that generation, each file in its newest
version at or before it. A file deleted before that generation still appears, because the
index cannot know when a name stopped existing, only when it was last seen.

Options: `--flat` (only `all/` at the root), `--at-gen N` (only generation N at the root),
`--no-analyze`, `--refresh SECONDS`, `--show-unreadable`, `-x NAME` / `--no-exclude`.

The mount is read-only and single-threaded; the kernel caches attributes, names and file pages
(for `--refresh` seconds), so browsing large folders stays fast. The first listing after
mounting loads the stored categories (about a second for 370k files).

### One-shot listing

```
uv run mbkn-btrfs-rescue ls -l /_work@257/project
```

## 5. Restore

```
uv run mbkn-btrfs-rescue restore /_work@257/project ~/recovered
uv run mbkn-btrfs-rescue restore --include damaged /_work@257/project ~/recovered
uv run mbkn-btrfs-rescue restore --at-gen 1150 /_work@257/project/app.py ~/recovered/old
```

Each file is written in its **best version** - the same one `best/` shows (an older version
when the newest is damaged, lost or empty); `--latest` writes the newest version instead.
By default only **intact** and **unverified** files (judged by the version written) are
written; `--include damaged`, `--include lost` or `--include all` add the others. Creates
`DEST/<name>/...`, preserves mtimes and symlinks, never overwrites unless `--overwrite`,
removes directories left empty by the filter, and writes `DEST/.mbkn-restore-<timestamp>.tsv`
listing every file with its category, action (`written` / `skipped` / `exists`), sector counts
and, for older versions, the generation used.

Copying from the mount's `best/` gives the same bytes; see the README for when to prefer
which.

Reconstructed files (see `best/` above) are written too: complete ones by default, partly filled
ones with `--include damaged`. The report marks them `reconstructed`, with their source and
every filled range as `start-end@generation`. `--no-patch` writes the files' own content
instead.

## 6. Review list

```
uv run mbkn-btrfs-rescue review                      # TSV to stdout, whole disk
uv run mbkn-btrfs-rescue review /_work@257 -o ~/review.tsv
uv run mbkn-btrfs-rescue review --include damaged,unverified,lost
```

One row per file (best version, as `best/` shows it; `--latest` for the newest): path,
category, size, bad and unverifiable byte counts, version, the byte ranges (`start-end`, up to
20 each) and the classification detail. Directories without such files are skipped, so a
whole-disk review takes seconds.

## 7. Lost files from git repositories

```
uv run mbkn-btrfs-rescue git-rescue /_work@257                       # report only (TSV)
uv run mbkn-btrfs-rescue git-rescue /_work@257 --dest ~/recovered-git  # also write files
```

`best/` and `restore` already use this automatically; `git-rescue` gives the full report
(including files git cannot help with: `not tracked`, `object lost`) and can export the blobs.

For each git work tree with lost or damaged files, the readable part of its `.git` is restored
to a scratch directory under `tmp_dir`, and the blobs recorded for each such file are read with
`git`: from the **index** (staged), the latest **stash**, and **HEAD**. Each blob is compared
with the lost file's recorded btrfs checksums: `verified` means byte-identical to the lost
version; otherwise the report says `same size` or `size differs` (an older version). Files are
written to `DEST/<subvol>@<id>/path` (never overwriting); report
`DEST/.mbkn-git-rescue-<timestamp>.tsv`. Needs `git`. The restored repository cannot run
anything: its config is replaced by a minimal one and hooks are not restored.

Restore to a **different disk** than the one being recovered.

## 8. Housekeeping

```
uv run mbkn-btrfs-rescue status        # mounts, running commands, progress, device, disk usage
uv run mbkn-btrfs-rescue umount        # unmount all of this tool's mounts (or name one)
uv run mbkn-btrfs-rescue stop          # unmount everything, pause running analyses
uv run mbkn-btrfs-rescue clean         # show what can be removed; --yes to delete
```

`status` lists this tool's FUSE mounts and running commands (only the tool itself - an editor
opened on the project directory never matches), the analysis state, whether the device is
readable and read-only at the block layer, and the size of the index, the sector-hash cache
and `tmp_dir`. Sizes never include what is mounted.

`umount` fails when something still uses the mount (a shell `cd`'ed into it, a file manager);
close it, or `--lazy` detaches at once and finishes when it is no longer used. `stop`
interrupts running `analyze`/`classify`/`match` commands with Ctrl-C semantics: they resume
where they stopped on the next run (`--force` sends SIGTERM).

`clean` removes only what the tool creates and can recreate: test images, scratch directories
of interrupted git steps, logs, unfinished hash files; `--hashes` adds the sector-hash cache
(rebuilding it takes minutes). The index and anything else in `tmp_dir` (restored files) are
listed as kept and never touched; nothing with a mount inside is deleted.

## Typical session for "recover my code, skip virtualenvs"

```
scripts/device-access.sh grant
uv run mbkn-btrfs-rescue mount ~/rescue
# browse ~/rescue/best/_work@257/... in a file manager, copy what you need, or:
uv run mbkn-btrfs-rescue restore /_work@257 ~/recovered      # everything good, with a report
uv run mbkn-btrfs-rescue git-rescue /_work@257 --dest ~/recovered-git   # lost code from git
uv run mbkn-btrfs-rescue review /_work@257 -o ~/review.tsv   # what is left to check
```
