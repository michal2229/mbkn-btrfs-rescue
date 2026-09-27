# How it works

## Why not `btrfs restore`?

`btrfs restore` (and most GUI tools) start from a tree root — the current one, a backup root
from the superblock, or one found by `btrfs-find-root` — and walk *down* the B-tree. After an
`rm -rf`, subvolume delete or a crash, the current roots no longer reference the lost files and
the older roots are often partially overwritten, so a single broken internal node hides the whole
subtree below it.

btrfs is copy-on-write: every change writes *new* tree blocks and leaves the old ones in place
until the space is reused. Those old blocks are self-describing — each carries the filesystem
UUID, its logical address, generation, owning tree id, and a checksum. So instead of walking
down from roots, this tool finds **every** tree block on the device and reassembles the
filesystem bottom-up from the leaves.

## Pipeline

```
 device (read-only)
   │
   │  scan     sequential sweep, every 4 KiB offset; numpy header filter + checksum
   ▼
 nodes table  (phys, logical, gen, owner tree, level, fsid, csum_ok, first/last key)
   │
   │  extract  parse leaves of one fsid: root tree, chunk tree, fs/subvolume trees
   │          (runs incrementally during the scan when the fsid is known)
   ▼
 item tables  chunks · roots · root_refs · inodes · dirents · extents
   │          (each item merged across all generations it was seen in: gen_min..gen_max)
   │
   │  classify verify every data sector against the csum tree (physical order)
   │          -> per-sector states
   │  match    hash every device sector once; bad sectors whose expected checksum is found
   │          next to their neighbour's -> sector patches (copies, zeros)
   ▼          -> per-file category, best version, per-directory mask
 RescueFS     namespace / path resolution / file layout (+ patches) / categories
   ├── shell       interactive browser
   ├── mount       read-only FUSE
   ├── restore     copy out + TSV report (--fill-older)
   ├── review      unverified/damaged files with bad byte ranges
   └── git-rescue  lost work-tree files from .git (index, stash, HEAD)
```

### scan

* Reads the device in 64 MiB chunks into three reused buffers; a background thread reads the
  next chunk while the current one is parsed (`posix_fadvise` sequential, page cache dropped
  behind).
* Every sector-aligned offset is tested with a vectorised filter on header fields (non-zero
  sector-aligned logical address, plausible generation, level < 8, item count fits the node).
* Survivors are checksum-verified (crc32c / xxhash64 / sha256 / blake2b, from the superblock).
* Valid blocks of **any** filesystem UUID are recorded, so blocks of a filesystem that was
  replaced by a new `mkfs` are found too (`fsids` shows them). Blocks with a bad checksum are
  recorded only for the UUIDs in the current superblocks (they may still hold usable items with
  `extract --allow-bad-csum`).
* Progress is committed after every chunk, so `scan --resume` continues after an interruption.

### extract

* Picks one filesystem UUID (the one with most valid blocks, or `--fsid`).
* Leaves of the chunk tree give the logical → physical mapping (plus the superblock's
  `sys_chunk_array`). Leaves of the root tree give subvolume names (`ROOT_BACKREF`).
* Leaves of filesystem trees (id 5 and ≥ 256) give `INODE_ITEM`, `DIR_ITEM`/`DIR_INDEX`,
  `INODE_REF`/`INODE_EXTREF` and `EXTENT_DATA` items.
* Every block is re-read and re-validated (UUID, generation, checksum) before parsing, so an
  index that no longer matches the device is detected rather than trusted.
* Items are **merged across generations**: an item seen in leaves of generations 900 and 1100 is
  stored once with `gen_min=900, gen_max=1100`. Deleted files survive as items that simply stop
  appearing in newer leaves.

### the namespace

* `/` lists every filesystem tree as `<name>@<tree id>`.
* A directory shows every name ever recorded for it; on a clash the newest wins.
* A name is **stale** (`~` in the shell, hidden by default) when the inode's newest known
  location is a different directory/name — i.e. it was renamed or moved.
* `.orphans/` in a subvolume root lists inodes that cannot be reached from the root (e.g. their
  parent directory's leaves were overwritten); they are named `<ino>_<last known name>`.
* Names in `exclude` (config / `-x`) are hidden everywhere — `.venv`, `.venv-tools` by default.
* A nested subvolume is listed once, at `/` as `<name>@<id>`; its entry inside the parent
  directory is not followed (it would duplicate every file below it).
* Hard links: an inode with several names is shown under its newest name; the others are stale.

### file content

Extent items are "painted" onto the file range, later ones covering earlier ones, then cut to
the inode size. The order:

1. the newest **metadata** generation the item was seen in (`gen_max`) - the item in the newest
   leaf describes the latest state of that range, even when it points at older data (reflink
   copies, dedupe and defragmentation reuse old extents, so ordering by the data's own
   generation alone gets such files wrong);
2. the extent's own generation;
3. data over holes / preallocated space from the same transaction (`fallocate` + write turns a
   prealloc item into a data item within one generation).

Painting is O(n log n), so files with tens of thousands of extent records stay fast. Inline,
regular, prealloc, holes and zlib / lzo / zstd compression are supported (decompression via
[`dissect.btrfs`](https://github.com/fox-it/dissect.btrfs)).

`--at-gen N` (or `gen N` in the shell) ignores items first seen after generation `N`, which
gives the file as it was at that point — use `versions PATH` to see which generations exist.

### categories

A file is judged only by the sectors it **references**: an uncompressed file range maps to a
sub-range of its extent (after partial overwrites or `fallocate`, a file often uses just part
of an extent), so a bad sector in an unused part does not count. Compressed extents are needed
whole. Per-sector results are stored for extents that mix good and bad sectors.

| category | rule |
|---|---|
| `intact` | every referenced sector matches its checksum, or the data is inline / empty |
| `unverified` | no mismatches, some sectors have no checksum on record - and the data is plausible (below) |
| `damaged` | some sectors are good, some bad |
| `lost` | nothing good: all sectors bad, no data extents recovered, or only holes |

Where no checksum survives, the data is still tested - these all count as **bad**:

* sectors that read as nothing but zeros,
* a compressed extent that does not decompress,
* a file whose first bytes contradict its type: magic numbers for common media, archive and
  git formats (`.png`, `.jpg`, `.webp`, `.wav`, `.mp4`, `.mkv`, `.zip`, `.zst`, `.pack`,
  `.pyc`, ...), valid UTF-8 without NUL bytes for text and source files (`.py`, `.js`, `.md`,
  `.json`, ...). Unknown types are not judged. With a single data extent the file is `lost`,
  with more it is `damaged` (later extents may be fine).

On the LUKS disk this was built for, 1,900 of 2,500 "no checksum" files turned out to be TRIM
garbage by these tests (random bytes where `.wav`, `.js`, `.md` data should be).

`classify` stores the category per file and a bitmask per directory ("which categories exist
below here"), so the FUSE category folders and the shell listing are instant.

### best version

For every file that is damaged, lost or empty in its newest version, `classify` also looks at
up to 64 older versions (the generations at which its extent items changed) and records the
newest one that is strictly better: a better category, fewer bad sectors among damaged
versions, or real content instead of an empty file. The mount's `best/` folder and `restore`
(default) serve that version; `stat` in the shell names it. Files with nothing readable in any
version are the only ones missing from `best/`.

### copies

A bad sector's expected checksum is still in the checksum tree, and the same data is often
somewhere else on the device: a plain `cp` of the file, another subvolume, the second copy of
DUP data, the old location of a chunk moved by a balance. `match` hashes every sector of the
device once (numpy array, one key per sector, built by worker processes at disk speed) and
looks the expected checksums up in it.

With crc32c (32 bits) chance matches are common: on a 500 GB disk (125M sectors) a given
checksum matches some unrelated sector with a probability of about 3% - on the real disk
1.9M of 67M bad sectors had such a match. A candidate therefore counts only when it is
**confirmed**: the bad sector's logical neighbour (previous or next sector, bad or good, in the
same or an adjacent extent) matches the device sector right next to the candidate. A chance
match passes that with a probability of about 2^-32. All-zero sectors never confirm (zeros are
everywhere). A lone sector (a file of one sector) cannot be confirmed and is not recovered this
way. With 64-bit or longer checksums (xxhash64, sha256, blake2b) every match counts. Each
chosen sector is re-read and verified against the full checksum before it is recorded.

A bad sector whose expected checksum is that of an all-zero sector held zeros (sparse
regions of images and databases); it is restored as zeros without reading anything.

Results are stored per extent (`sector_patch`: for each sector, where to read it instead) and
applied when reading and categorising: a patched sector counts as verified. `stat` shows
`N sectors from copies` per extent, the file detail `(N recovered from copies)`.

### git repositories

Source code whose data blocks were discarded is often still inside `.git`, compressed in
object files and packs (so `match` cannot see it). `git-rescue` restores the readable part of
each affected repository's `.git` to a scratch directory and asks `git` for the blobs of every
lost or damaged path in the index (staged), the latest stash and HEAD; each blob is re-hashed
to its object id. A blob is then compared with the lost file's expected sector checksums
(uncompressed extents): if all comparable sectors match, it *is* the lost content.

### verification (per extent)

For each data extent the tool looks up the checksum tree leaves written at or after the
extent's generation and compares per-sector checksums:

| status     | meaning |
|------------|---------|
| `ok`       | all sectors match the recorded checksums |
| `inline`   | data stored inside the metadata leaf (already covered by the leaf checksum) |
| `partial`  | some sectors verified, no mismatches, the rest have no checksum on record |
| `nocsum`   | no checksum found (nodatasum file, or checksum leaves overwritten) — inspect manually |
| `bad`      | mismatch: the space was reused, the data is probably overwritten |
| `zeroed`   | mismatch and the extent reads as all zeros — typically discarded by TRIM |
| `unmapped` | the logical address is not covered by any recovered chunk |

`, N sectors from copies` is appended when `match` replaced bad sectors of the extent.

**Encrypted devices (LUKS / dm-crypt):** a TRIMmed block reads as zeros *below* the encryption
layer, which decrypts to random-looking bytes. On a LUKS device, discarded data therefore
shows up as `bad`, not `zeroed`. If nearly all old extent-based files are `bad` while very
little was written after the loss, discard (btrfs `discard=async`, the Fedora default, plus
`allow-discards` / `discard` in crypttab) is the likely cause. That data is gone. Files small
enough to be stored **inline** in metadata (≤ 2 KiB by default, `max_inline`) live in tree
leaves and survive, as do all names, sizes and timestamps.

`restore` writes only intact and unverified files by default (best version of each);
`--include damaged|lost|all` widens that, `--latest` uses the newest version.

## Performance

Measured on a 477 GiB NVMe behind LUKS (1.6 GB/s raw reads), 250k tree blocks, 1.6M extent
records, 370k files:

| step | time |
|---|---|
| scan | ~1.2 GiB/s (read-ahead thread, reused buffers) |
| extract | ~30 s (also runs incrementally during the scan) |
| classify: checking data | disk speed (~570 MiB/s, physical order); later runs re-check only new or changed-rule extents |
| classify: categorising + best versions | ~25 s |
| match: hashing the device (once, cached) | ~1.3 GiB/s (6.5 min for 477 GiB) |
| match: 67M bad sectors in 714k extents | ~5 min |
| mount: first listing | ~1 s (loads stored categories), then ~0.1 ms per entry |

Directory listings, the subvolume list and file layouts are cached per index snapshot; the
kernel caches attributes and pages of the read-only mount.

## Limitations

* See [compatibility.md](compatibility.md) for supported btrfs features and profiles.
* Space that was reused or TRIMmed is gone; the scan can only find what is still on the disk.
* Encrypted (fscrypt) files are not decrypted.
* xattrs/ACLs are not restored; hard links are restored as separate files (newest name only).
* The plausibility tests for unchecksummed data are heuristics: random-looking content of an
  unknown or naturally random type (compressed media without a known signature) stays
  `unverified`. Check such files before trusting them.
* `best/` picks whole versions; it does not combine good parts of different versions into one
  file (that would produce content that never existed). `restore --fill-older` does, on
  request, and lists every filled range. Identical content from other versions is already
  used automatically (`match` finds it by checksum).
* With crc32c, copies of single-sector data cannot be confirmed and are not used.
* `git-rescue` needs the repository's objects to be readable; blobs in damaged packs are
  skipped (every blob is re-hashed).
