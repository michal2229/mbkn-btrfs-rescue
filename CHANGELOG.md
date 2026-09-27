# Changelog

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
