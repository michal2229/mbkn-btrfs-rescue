"""SQLite index schema.

`nodes` is filled by `scan` (one row per tree block found on the device, every generation).
The item tables are filled by `extract` from leaves of the selected filesystem (fsid).
Items are merged across generations: gen_min/gen_max record in which leaf generations a given
item was seen, so deleted/overwritten entries are kept alongside current ones.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS nodes (
    phys INTEGER PRIMARY KEY,   -- byte offset on the device
    bytenr INTEGER NOT NULL,    -- logical address from the header
    gen INTEGER NOT NULL,
    owner INTEGER NOT NULL,     -- tree id (stored as signed 64-bit)
    level INTEGER NOT NULL,
    nritems INTEGER NOT NULL,
    fsid BLOB NOT NULL,
    csum_ok INTEGER NOT NULL,
    first_obj INTEGER, first_type INTEGER, first_off INTEGER,
    last_obj INTEGER, last_type INTEGER, last_off INTEGER
);
CREATE INDEX IF NOT EXISTS nodes_owner ON nodes (fsid, owner, level, first_off);
CREATE INDEX IF NOT EXISTS nodes_bytenr ON nodes (bytenr, gen);

CREATE TABLE IF NOT EXISTS chunks (
    logical INTEGER, gen INTEGER, length INTEGER, type INTEGER,
    num_stripes INTEGER, stripes TEXT,  -- "devid:phys,devid:phys"
    PRIMARY KEY (logical, gen)
);

CREATE TABLE IF NOT EXISTS roots (
    tree INTEGER, koff INTEGER, gen INTEGER,     -- gen = leaf generation
    bytenr INTEGER, level INTEGER, root_gen INTEGER, refs INTEGER, drop_level INTEGER,
    PRIMARY KEY (tree, koff, gen)
);

CREATE TABLE IF NOT EXISTS root_refs (
    child INTEGER, parent INTEGER, dirid INTEGER, name BLOB,
    gen_min INTEGER, gen_max INTEGER,
    PRIMARY KEY (child, parent, dirid, name)
);

CREATE TABLE IF NOT EXISTS inodes (
    tree INTEGER, ino INTEGER, gen INTEGER,      -- gen = leaf generation
    created INTEGER, transid INTEGER, size INTEGER, nbytes INTEGER, mode INTEGER,
    nlink INTEGER, uid INTEGER, gid INTEGER, atime REAL, mtime REAL, ctime REAL,
    PRIMARY KEY (tree, ino, gen)
);

-- Directory edges from DIR_ITEM, DIR_INDEX, INODE_REF and INODE_EXTREF.
-- child_kind = 1 (inode) or 132 (subvolume link: child is a tree id).
CREATE TABLE IF NOT EXISTS dirents (
    tree INTEGER, dir INTEGER, name BLOB, child INTEGER, child_kind INTEGER, ftype INTEGER,
    gen_min INTEGER, gen_max INTEGER,
    PRIMARY KEY (tree, dir, name, child, child_kind)
);
CREATE INDEX IF NOT EXISTS dirents_child ON dirents (tree, child);

CREATE TABLE IF NOT EXISTS extents (
    tree INTEGER, ino INTEGER, foff INTEGER,
    egen INTEGER,           -- generation the extent data was written
    etype INTEGER, comp INTEGER, enc INTEGER, ram_bytes INTEGER,
    disk_bytenr INTEGER, disk_len INTEGER, eoff INTEGER, nbytes INTEGER,
    inline BLOB,
    gen_min INTEGER, gen_max INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS extents_key ON extents
    (tree, ino, foff, egen, etype, comp, disk_bytenr, eoff, nbytes);

-- Filled by `classify`: per data extent, sector checksum results.
CREATE TABLE IF NOT EXISTS extent_status (
    disk_bytenr INTEGER, disk_len INTEGER, egen INTEGER,
    good INTEGER, bad INTEGER, nocsum INTEGER, zero INTEGER, mapped INTEGER,
    sectors BLOB,  -- zlib(one byte per sector: 0 ok, 1 bad, 2 no checksum); NULL if uniform
    PRIMARY KEY (disk_bytenr, disk_len, egen)
);

-- Filled by `classify` for the latest view: file categories and directory category masks.
CREATE TABLE IF NOT EXISTS file_cat (
    tree INTEGER, ino INTEGER, cat TEXT, size INTEGER, PRIMARY KEY (tree, ino)
);
-- Files whose best content is an older version: read them as of generation `gen`.
CREATE TABLE IF NOT EXISTS best_version (
    tree INTEGER, ino INTEGER, gen INTEGER, cat TEXT, PRIMARY KEY (tree, ino)
);
-- Filled by `match`: per extent, where to read bad sectors from instead (identical copies).
-- patch = zlib(int64 physical offset per sector (-1 none) + uint8 kind per sector).
CREATE TABLE IF NOT EXISTS sector_patch (
    disk_bytenr INTEGER, disk_len INTEGER, egen INTEGER,
    copied INTEGER, zeros INTEGER, patch BLOB,
    PRIMARY KEY (disk_bytenr, disk_len, egen)
);
CREATE TABLE IF NOT EXISTS node_mask (
    tree INTEGER, ino INTEGER, mask INTEGER, PRIMARY KEY (tree, ino)  -- ino -1 = .orphans
);
"""

ITEM_TABLES = ("chunks", "roots", "root_refs", "inodes", "dirents", "extents")
CLASSIFY_TABLES = ("extent_status", "file_cat", "node_mask", "best_version", "sector_patch")

U64 = 1 << 64


def s64(v: int) -> int:
    """Map an unsigned 64-bit value into SQLite's signed INTEGER range."""
    return v - U64 if v >= 1 << 63 else v


def u64(v: int) -> int:
    return v + U64 if v < 0 else v


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Bring indexes made by older versions up to date (additive changes only)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(extent_status)")}
    if "sectors" not in cols:  # 0.1: per-extent counts only; mixed extents get re-checked
        con.execute("ALTER TABLE extent_status ADD COLUMN sectors BLOB")
        con.commit()
    if "weak" in {r[1] for r in con.execute("PRAGMA table_info(sector_patch)")}:
        # 0.3 development builds kept unconfirmed matches: drop them, `match` runs again
        con.execute("DROP TABLE sector_patch")
        con.execute("DELETE FROM meta WHERE key = 'patch_serial'")
        con.executescript(SCHEMA)
        con.commit()


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
