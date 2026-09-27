# Compatibility

btrfs has no on-disk format version. The format has been stable since Linux 3.x and is
extended through **feature flags** in the superblock (`incompat` = older code must not
mount it, `compat_ro` = older code may mount it read-only). This tool parses tree blocks
directly, so it depends only on those flags and the geometry, never on the running kernel.

Check a device with:

```bash
uv run mbkn-btrfs-rescue info      # superblocks, devid, feature flags, backup roots
```

Every command prints `warning: ...` lines on stderr for anything unsupported, and the FUSE
mount repeats them at the top of `README.txt`. No warnings = fully supported.

## Feature flags

| flag (incompat) | since Linux | status |
|---|---|---|
| `mixed_backref`, `default_subvol`, `big_metadata` | 2.6.x–3.x | ok (defaults) |
| `extended_iref` | 3.7 | ok (hard links via INODE_EXTREF) |
| `skinny_metadata` | 3.10 | ok (extent tree is not used) |
| `no_holes` | 3.14 | ok (missing ranges read as holes) |
| `compress_lzo` / `compress_zstd` | 2.6.38 / 4.14 | ok (decoded via dissect.btrfs) |
| `metadata_uuid` | 5.0 | ok (tree blocks matched by metadata UUID) |
| `raid1c34` | 5.5 | ok if a copy is on the given device |
| `simple_quota` | 6.7 | ok (quota trees ignored) |
| `mixed_groups` | 2.6.37 | warning: untested (small filesystems, `mkfs -M`) |
| `zoned` | 5.12 | warning: untested |
| `raid56` | 3.9 | **unsupported**: parity-striped data cannot be reassembled |
| `raid_stripe_tree` | 6.7 | **unsupported**: mapping lives in a tree that is not read |
| `extent_tree_v2` | experimental | **unsupported** |
| unknown bits | — | warning: newer than this tool, results unreliable |

`compat_ro` flags `free_space_tree(_valid)` (4.5), `verity` (5.15) and `block_group_tree`
(6.1) are all fine: those trees are not needed for recovery.

## Profiles and devices

Only the device given is read. Data is located via chunk items (logical → physical):

* `single`, `DUP`: fully supported (DUP metadata gives two chances per tree block).
* `RAID1`, `RAID1C3`, `RAID1C4`: works for chunks with a copy on this device (`devid` from
  its superblock); give each member device a separate index if needed.
* `RAID0`, `RAID10`, `RAID5`, `RAID6`: extents in striped chunks report `unmapped`/errors.
* Multi-device filesystems get a warning; chunks stored only on other devices are
  reported as "stored on another device only".

## Checksums, compression, geometry

* Checksums: crc32c (default), xxhash64, sha256, blake2b (Linux 5.5+).
* Compression: zlib, lzo, zstd, including per-sector LZO framing.
* Sector size 4096 is tested; node sizes 4–64 KiB work (the scan checks every 4 KiB).
* fscrypt-encrypted files are listed with names/metadata but their content is not decrypted.
  LUKS/dm-crypt below btrfs is transparent (open the mapping first).

## Environment

* Linux; Python ≥ 3.14; FUSE mount needs `fuse3` (`fusermount3`).
* Tested with kernel 7.2, btrfs-progs 7.1 (test images via `mkfs.btrfs`).
