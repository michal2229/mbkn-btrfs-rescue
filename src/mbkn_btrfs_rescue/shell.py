"""Interactive browser for the recovered namespace (cmd + readline, tab completion)."""

from __future__ import annotations

import cmd
import fnmatch
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import ondisk as od
from .model import (
    CAT_BITS,
    CAT_DAMAGED,
    CAT_HELP,
    CAT_INTACT,
    CAT_LOST,
    CATEGORIES,
    ROOT,
    Entry,
    RescueFS,
)
from .restore import DEFAULT_CATEGORIES, restore

CAT_MARK = {"intact": "✓", "unverified": "?", "damaged": "!", "lost": "✗"}
TYPE_CHAR = {od.FT_DIR: "d", od.FT_SYMLINK: "l", od.FT_REG: "-"}


def fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


class Shell(cmd.Cmd):
    intro = (
        "Markers: ✓ intact  ? unverified  ! damaged  ✗ lost (hidden, `ls -a`)  ~ stale name\n"
        + "mbkn-btrfs-rescue shell - read-only. Type `help` for commands, "
        "`cd <TAB>` to start.\nNames marked `~` are stale (the file lives elsewhere "
        "in a newer generation)."
    )

    def __init__(self, fs: RescueFS, start: tuple[Entry, ...] = ()):
        super().__init__()
        self.fs = fs
        self.cwd: tuple[Entry, ...] = start
        self._update_prompt()

    # ------------------------------------------------------------------ helpers

    def _update_prompt(self) -> None:
        gen = f" gen<={self.fs.at_gen}" if self.fs.at_gen is not None else ""
        self.prompt = f"rescue{gen}:{self.fs.chain_path(self.cwd)}> "

    def _resolve(self, path: str) -> tuple[Entry, ...] | None:
        try:
            return self.fs.resolve(path or ".", self.cwd)
        except FileNotFoundError as err:
            print(f"not found: {err}")
            return None

    def _args(self, line: str) -> list[str] | None:
        try:
            return shlex.split(line)
        except ValueError as err:
            print(f"parse error: {err}")
            return None

    def _complete_path(self, text: str, line: str, begidx: int, dirs_only=False) -> list[str]:
        # readline splits on spaces/slashes differently; complete on the whole last token
        token = line[:begidx].split(" ")[-1] + text
        head, _, tail = token.rpartition("/")
        try:
            chain = self.fs.resolve(head or ("/" if token.startswith("/") else "."), self.cwd)
        except FileNotFoundError:
            return []
        node = chain[-1].node if chain else None

        kids = self.fs.children(node or ROOT)
        out = []
        for name, e in kids.items():
            if name.startswith(tail) and (e.is_dir or not dirs_only):
                full = name + ("/" if e.is_dir else "")
                out.append(full[len(tail) - len(text) :] if len(tail) >= len(text) else full)
        return out

    def complete_cd(self, text, line, begidx, endidx):
        return self._complete_path(text, line, begidx, dirs_only=True)

    def completedefault(self, text, line, begidx, endidx):
        return self._complete_path(text, line, begidx)

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> None:
        print(f"unknown command: {line.split()[0]} (try `help`)")

    # ------------------------------------------------------------------ navigation

    def do_pwd(self, _line: str) -> None:
        """pwd - print the current path"""
        print(self.fs.chain_path(self.cwd))

    def do_cd(self, line: str) -> None:
        """cd PATH - change directory (`/` lists subvolumes, `..` goes up)"""
        chain = self._resolve(line.strip() or "/")
        if chain is None:
            return
        if chain and not chain[-1].is_dir:
            print("not a directory")
            return
        self.cwd = chain
        self._update_prompt()

    def do_ls(self, line: str) -> None:
        """ls [-l] [-a] [PATH] - list a directory (-l: details, -a: include stale names)"""
        args = self._args(line)
        if args is None:
            return
        long = "-l" in args or "-la" in args or "-al" in args
        show_all = "-a" in args or "-la" in args or "-al" in args
        paths = [a for a in args if not a.startswith("-")]
        chain = self._resolve(paths[0] if paths else ".")
        if chain is None:
            return

        node = chain[-1].node if chain else ROOT
        if chain and not chain[-1].is_dir:
            self._print_entries([chain[-1]], long)
            return
        if node == ROOT:
            for t, name, count in self.fs.trees():
                print(f"  {name}@{t:<8}  {count:>9} inodes")
            return
        kids = self.fs.children(node)
        entries = [e for _, e in sorted(kids.items()) if show_all or self._shown(e)]
        hidden = len(kids) - len(entries)
        self._print_entries(entries, long)
        if hidden:
            print(f"  ({hidden} stale or lost entries hidden, use ls -a)")

    def _shown(self, e: Entry) -> bool:
        """Default listing: hide stale names and anything containing only lost files."""
        if e.stale:
            return False
        if not self.fs.masks_valid():
            return True
        mask = self.fs.entry_mask(e)
        return mask == 0 or bool(mask & ~CAT_BITS[CAT_LOST])

    def _mark(self, e: Entry) -> str:
        if e.stale:
            return "~"
        if not self.fs.masks_valid():
            return " "
        if not e.is_dir:
            return CAT_MARK.get(self.fs.category(e), " ")
        mask = self.fs.entry_mask(e)
        best = next((c for c in CATEGORIES if mask & CAT_BITS[c]), None)
        return CAT_MARK.get(best, " ")

    def _print_entries(self, entries: list[Entry], long: bool) -> None:
        for e in entries:
            mark = self._mark(e)
            name = e.name + ("/" if e.is_dir else "")
            if not long:
                print(f" {mark}{name}")
                continue
            st = self.fs.stat(e)
            size = "" if e.is_dir else fmt_size(st["st_size"])
            print(
                f" {mark}{TYPE_CHAR.get(e.ftype, '?')} {size:>8}  {fmt_time(st['st_mtime'])}"
                f"  gen {e.gen_min}-{e.gen_max}  ino {e.node.ino:<8} {name}"
            )

    def _walk_from(self, chain: tuple[Entry, ...]):
        """Yield (absolute path, entry) below chain; at `/` walk every subvolume."""
        if chain:
            tops = [(self.fs.chain_path(chain), chain[-1])]
        else:
            tops = [("/" + n, e) for n, e in sorted(self.fs.children(ROOT).items())]
        for base, top in tops:
            for rel, e in self.fs.walk(top, ""):
                yield base + rel[len(top.name) :], e

    def do_tree(self, line: str) -> None:
        """tree [PATH] [DEPTH] - recursive listing (default depth 3)"""
        args = self._args(line) or []
        nums = [a for a in args if a.isdigit()]
        paths = [a for a in args if not a.isdigit()]
        depth = int(nums[0]) if nums else 3
        chain = self._resolve(paths[0] if paths else ".")
        if chain is None:
            return
        base = self.fs.chain_path(chain).rstrip("/")
        for path, e in self._walk_from(chain):
            level = path[len(base) :].count("/")
            if level <= depth:
                print("  " * (level - 1) + e.name + ("/" if e.is_dir else ""))

    def do_find(self, line: str) -> None:
        """find GLOB [PATH] - find names matching a glob below PATH (default: cwd)"""
        args = self._args(line)
        if not args:
            print("usage: find GLOB [PATH]")
            return
        chain = self._resolve(args[1] if len(args) > 1 else ".")
        if chain is None:
            return
        for path, e in self._walk_from(chain):
            if fnmatch.fnmatch(e.name, args[0]):
                print(path + ("/" if e.is_dir else ""))

    def do_grep(self, line: str) -> None:
        """grep REGEX [PATH] - search file contents (files < 4 MiB) below PATH"""
        args = self._args(line)
        if not args:
            print("usage: grep REGEX [PATH]")
            return
        rx = re.compile(args[0].encode())
        chain = self._resolve(args[1] if len(args) > 1 else ".")
        if chain is None:
            return
        entries = (
            [(self.fs.chain_path(chain), chain[-1])]
            if chain and not chain[-1].is_dir
            else self._walk_from(chain)
        )
        for path, e in entries:
            if e.is_dir or e.ftype == od.FT_SYMLINK:
                continue
            if self.fs.layout(e.node.tree, e.node.ino).size > 4 << 20:
                continue
            data = self.fs.read(e.node.tree, e.node.ino, 0, 4 << 20)
            for n, text in enumerate(data.splitlines(), 1):
                if rx.search(text):
                    print(f"{path}:{n}: {text.decode('utf-8', 'replace')[:200]}")

    # ------------------------------------------------------------------ inspection

    def do_stat(self, line: str) -> None:
        """stat PATH - inode details, extents and verification status"""
        chain = self._resolve(line.strip())
        if not chain:
            return
        e = chain[-1]
        t, ino = e.node.tree, e.node.ino
        print(
            f"path     {self.fs.chain_path(chain)}\nsubvol   {self.fs.tree_label(t)}  ino {ino}"
            f"\ntype     {od.FT_NAMES.get(e.ftype, '?')}  seen in gens {e.gen_min}-{e.gen_max}"
            + ("  (stale name)" if e.stale else "")
        )
        info = self.fs.inode(t, ino)
        if info:
            print(
                f"size     {info.size}  mode {oct(info.mode)}  nlink {info.nlink}  "
                f"uid {info.uid}  gid {info.gid}\nmtime    {fmt_time(info.mtime)}  "
                f"created gen {info.created}  last change gen {info.transid}"
            )
        else:
            print("inode item not recovered (size inferred from extents)")
        if not e.is_dir:
            lay = self.fs.layout(t, ino)
            print(f"extents  {len(lay.segments)} segments, size {lay.size}")
            for a, b, r in lay.segments[:20]:
                kind = (
                    "inline"
                    if r.etype == od.FILE_EXTENT_INLINE
                    else "hole"
                    if not r.is_data
                    else f"@{r.disk_bytenr:#x}+{r.disk_len}"
                )
                comp = od.COMPRESS_NAMES.get(r.comp, "?")
                print(f"  [{a}, {b})  gen {r.egen}  {kind}  {comp}  {self.fs.verify_extent(r)}")
            if len(lay.segments) > 20:
                print(f"  ... {len(lay.segments) - 20} more")
            cat, detail = self.fs.file_check(t, ino)
            print(f"category {cat}  ({detail})")
            gen, best_cat = self.fs.best_version(t, ino)
            if gen is not None:
                print(f"best     older version at generation {gen} is {best_cat} (see best/)")

    def do_versions(self, line: str) -> None:
        """versions PATH - generations of the inode seen on disk (use with `gen N`)"""
        chain = self._resolve(line.strip())
        if not chain:
            return
        e = chain[-1]
        vs = self.fs.versions(e.node.tree, e.node.ino)
        if not vs:
            print("no inode items recovered")
        for v in vs:
            print(
                f"  seen gen {v.gen:<8} changed gen {v.transid:<8} size {v.size:<10} "
                f"mtime {fmt_time(v.mtime)}"
            )
        egens = sorted({r.egen for r in self.fs.extent_rows(e.node.tree, e.node.ino)})
        if egens:
            print(f"  extent write generations: {egens}")

    def do_cat(self, line: str) -> None:
        """cat PATH - print file content"""
        self._show(line, pager=False)

    def do_less(self, line: str) -> None:
        """less PATH - view file content in $PAGER (default: less)"""
        self._show(line, pager=True)

    def _show(self, line: str, pager: bool) -> None:
        chain = self._resolve(line.strip())
        if not chain or chain[-1].is_dir:
            print("not a file")
            return
        e = chain[-1]
        data = b"".join(self.fs.iter_content(e.node.tree, e.node.ino))
        if pager:
            pager_cmd = shlex.split(os.environ.get("PAGER", "less -R"))
            subprocess.run(pager_cmd, input=data, check=False)
        else:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
            if data and not data.endswith(b"\n"):
                print()

    # ------------------------------------------------------------------ actions

    def do_restore(self, line: str) -> None:
        """restore PATH DEST [--overwrite] [--damaged] [--lost] [--latest] - copy to DEST

        Writes intact and unverified files, each in its best version (an older one when the
        newest is broken); --damaged / --lost include those too, --latest keeps newest."""
        args = self._args(line)
        if not args or len([a for a in args if not a.startswith("--")]) != 2:
            print("usage: restore PATH DEST [--overwrite] [--damaged] [--lost] [--latest]")
            return
        src, dst = [a for a in args if not a.startswith("--")]
        chain = self._resolve(src)
        if not chain:
            print("restore: pick a path inside a subvolume")
            return
        cats = set(DEFAULT_CATEGORIES)
        cats |= {c for c in (CAT_DAMAGED, CAT_LOST) if f"--{c}" in args}
        dest = Path(dst).expanduser()
        stats, report = restore(
            self.fs,
            chain[-1],
            dest,
            categories=frozenset(cats),
            overwrite="--overwrite" in args,
            best="--latest" not in args,
        )
        print(
            f"wrote {stats.files} files ({fmt_size(stats.bytes)}), {stats.links} symlinks "
            f"({stats.older_versions} older versions); "
            f"skipped {stats.skipped_category} by category, {stats.skipped} existing"
        )
        print(f"categories seen: {stats.by_category}\nreport: {report}")

    def do_summary(self, line: str) -> None:
        """summary [PATH] - count files per category below PATH"""
        chain = self._resolve(line.strip() or ".")
        if chain is None:
            return
        counts: dict[str, list[int]] = {c: [0, 0] for c in CATEGORIES}
        for _path, e in self._walk_from(chain):
            if e.is_dir:
                continue
            cat = self.fs.category(e) or CAT_INTACT
            counts[cat][0] += 1
            counts[cat][1] += self.fs.layout(e.node.tree, e.node.ino).size
        for cat in CATEGORIES:
            n, size = counts[cat]
            print(f"  {CAT_MARK[cat]} {cat:11} {n:>8} files {fmt_size(size):>9}   {CAT_HELP[cat]}")

    def do_gen(self, line: str) -> None:
        """gen [N|off] - view the filesystem as of generation N (older versions)"""
        arg = line.strip()
        if not arg:
            print(f"at_gen = {self.fs.at_gen}")
            return
        self.fs.at_gen = None if arg in ("off", "none", "latest") else int(arg)
        self.fs._layouts.clear()
        self._update_prompt()

    def do_exclude(self, line: str) -> None:
        """exclude [NAME ...] | exclude -NAME - show / add / remove hidden names"""
        for arg in line.split():
            if arg.startswith("-"):
                self.fs.exclude.discard(arg[1:])
            else:
                self.fs.exclude.add(arg)
        self.fs._orphans.clear()
        print("excluded:", ", ".join(sorted(self.fs.exclude)) or "(none)")

    def do_quit(self, _line: str) -> bool:
        """quit - leave the shell"""
        return True

    do_exit = do_quit
    do_EOF = do_quit
