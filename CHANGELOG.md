# Changelog

## 0.3.0

### Recovery
- **Copies** (`match`, issue #1): every device sector is hashed once (cached); bad sectors
  whose expected checksum is found right next to their neighbour's are read from that copy,
  sectors whose checksum is that of zeros come back as zeros. Runs in `classify`, `analyze`
  and `mount` by default (`--no-match`). Chance crc32c matches are never used.
- **`git-rescue`** (#3): lost/damaged files of git work trees from the index, stash or HEAD,
  compared with the lost file's checksums (`verified` = byte-identical); report and
  `--dest` to write them. The restored repository never runs hooks or config commands.
- **`review`** (#4): TSV of unverified and damaged files with bad/unverifiable byte ranges.
- **`restore --fill-older`** (#2): fills bad ranges of damaged files from older versions,
  listing each filled range in the report. (Identical content of other versions is used
  automatically by `match`.)

### Other
- CI on GitHub Actions (#5): lint + tests in a Fedora container.
- `stat` shows sectors recovered from copies per extent.

### Upgrading from 0.2
Indexes are migrated in place. `mount` (or `classify`) runs the copy search once; hashing the
device takes a few minutes and needs `4 bytes x sectors` of cache space (500 MB per 500 GB).

## 0.2.0

First release on `main` (0.1 was only developed on the `init` branch).

### Recovery
- **`best/`** mount folder and `restore` default: the newest version of each file, or an
  older one when the newest is damaged, lost or empty (up to 64 versions; fewer bad sectors
  wins among damaged ones). `restore --latest` for the newest version; `stat` shows it.
- File content follows the newest metadata describing a range (fixes files copied with
  reflinks, deduplicated or defragmented, and `fallocate` + write).
- Files are judged by the sectors they actually use (per-sector results for mixed extents).
- Unchecksummed data that reads as zeros, does not decompress, or contradicts its file type
  (magic numbers, UTF-8 text) no longer counts as `unverified`.
- `all/` and `history/` hide files without recoverable data (`--show-unreadable`).
- Nested subvolumes appear once, at the top level (they were duplicated inside the parent).
- Symlinks are counted in the category totals.
- Compatibility checks: feature flags, checksum type, sector size, multi-device; warnings on
  every command and in the mount's README.txt; mapping uses only this device's stripes.

### Performance
- Scan about 2x faster (read-ahead thread, reused buffers).
- Categorisation minutes -> seconds (O(n log n) extent painting, in-memory checksum index).
- Mount: cached subvolume list and directory listings, kernel attribute/page caching.

### Upgrading from 0.1
Indexes are migrated in place. Run `classify` (or just `mount`) once: it re-checks only the
extents whose rules changed and recomputes the categories.

## 0.1.0

Scan / extract / shell / restore, categories (intact, unverified, damaged, lost), live FUSE
mount with category folders and per-generation history.
