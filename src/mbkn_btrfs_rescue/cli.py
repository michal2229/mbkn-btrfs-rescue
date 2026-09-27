"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

from . import __version__
from . import ondisk as od
from .checksum import CSUM_NAMES
from .compat import device_warnings, flag_names
from .config import Config, load_config
from .db import connect, get_meta, u64
from .device import Device, DeviceError


def parse_size(text: str) -> int:
    text = text.strip().upper().removesuffix("B").removesuffix("I")
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if text and text[-1] in mult:
        return int(float(text[:-1]) * mult[text[-1]])
    return int(text, 0)


def fmt_uuid(raw: bytes) -> str:
    return str(uuid.UUID(bytes=raw))


# ---------------------------------------------------------------------------- helpers


def _device(args, cfg: Config) -> Device:
    path = args.device or cfg.device
    if not path:
        raise SystemExit("no device given: use --device or set `device` in the config file")
    dev = Device(path)
    for w in device_warnings(dev.superblocks()):
        print(f"warning: {w}", file=sys.stderr)
    return dev


def _db(args, cfg: Config):
    path = Path(args.db) if args.db else cfg.db_path
    return connect(path), path


def _open_fs(args, cfg: Config, dev: Device):
    from .model import RescueFS

    con, _ = _db(args, cfg)
    return RescueFS(con, dev, exclude=_exclude(args, cfg), at_gen=getattr(args, "at_gen", None))


def _best_superblock(dev: Device) -> od.Superblock | None:
    valid = [sb for sb in dev.superblocks() if sb.valid_magic]
    return max(valid, key=lambda s: s.generation) if valid else None


# ---------------------------------------------------------------------------- commands


def cmd_info(args, cfg):
    with _device(args, cfg) as dev:
        print(
            f"device   {dev.path}  ({dev.size / 2**30:.2f} GiB, "
            f"{'block device' if dev.is_block else 'file'})"
        )
        for sb in dev.superblocks():
            if not sb.valid_magic:
                print(f"\nsuperblock @ {sb.offset:#x}: no btrfs magic")
                continue
            print(
                f"\nsuperblock @ {sb.offset:#x}\n"
                f"  label '{sb.label}'  fsid {fmt_uuid(sb.fsid)}"
                + (
                    f"  metadata_uuid {fmt_uuid(sb.metadata_uuid)}"
                    if sb.header_fsid != sb.fsid
                    else ""
                )
                + f"\n  generation {sb.generation}  total {sb.total_bytes / 2**30:.2f} GiB  "
                f"used {sb.bytes_used / 2**30:.2f} GiB  devices {sb.num_devices}\n"
                f"  nodesize {sb.nodesize}  sectorsize {sb.sectorsize}  "
                f"csum {CSUM_NAMES.get(sb.csum_type, sb.csum_type)}\n"
                f"  devid {sb.devid}  features: "
                f"{', '.join(flag_names(sb.incompat_flags, od.INCOMPAT_FLAGS)) or '-'}"
                f" | ro: {', '.join(flag_names(sb.compat_ro_flags, od.COMPAT_RO_FLAGS)) or '-'}\n"
                f"  root tree @ {sb.root:#x} (level {sb.root_level})  "
                f"chunk tree @ {sb.chunk_root:#x}"
            )
            for i, b in enumerate(sb.backups):
                if b.tree_root:
                    print(
                        f"  backup[{i}] gen {b.tree_root_gen}: root {b.tree_root:#x}  "
                        f"fs {b.fs_root:#x}"
                    )
    return 0


def cmd_scan(args, cfg):
    from .scan import ScanParams, scan

    with _device(args, cfg) as dev:
        sb = _best_superblock(dev)
        params = ScanParams(
            nodesize=args.nodesize or (sb.nodesize if sb else 16384),
            sectorsize=args.sectorsize or (sb.sectorsize if sb else 4096),
            csum_type=args.csum_type if args.csum_type is not None else (sb.csum_type if sb else 0),
            start=parse_size(args.start),
            end=parse_size(args.end) if args.end else None,
            chunk_bytes=args.chunk_mb << 20,
        )
        if args.fsid != "any":
            params.fsids = {bytes.fromhex(args.fsid.replace("-", ""))}
        params.keep_bad_fsids = {s.header_fsid for s in dev.superblocks() if s.valid_magic}
        if params.fsids:
            params.keep_bad_fsids |= params.fsids
        con, path = _db(args, cfg)
        if not args.resume and get_meta(con, "scan_pos") and not args.force:
            raise SystemExit(f"{path} already has scan data; use --resume or --force")
        if args.force and not args.resume:
            con.execute("DELETE FROM nodes")
            con.execute("DELETE FROM meta")
        print(
            f"scanning {dev.path} -> {path}\n  nodesize {params.nodesize}  sectorsize "
            f"{params.sectorsize}  csum {CSUM_NAMES.get(params.csum_type)}  fsid "
            f"{args.fsid}",
            file=sys.stderr,
        )
        found = scan(dev, con, params, resume=args.resume)
        print(f"found {found} tree blocks")
    return cmd_fsids(args, cfg)


def cmd_fsids(args, cfg):
    from .extract import fsid_summary

    con, _ = _db(args, cfg)
    print(f"{'fsid':36}  {'valid':>9}  {'bad csum':>9}  generations")
    for fsid, ok, bad, gmin, gmax in fsid_summary(con):
        print(f"{fmt_uuid(fsid)}  {ok:>9}  {bad:>9}  {gmin}-{gmax}")
    return 0


def cmd_trees(args, cfg):
    con, _ = _db(args, cfg)
    fsid = get_meta(con, "fsid")
    where, params = ("WHERE fsid=?", (bytes.fromhex(fsid),)) if fsid else ("", ())
    names = {
        c: n.decode(errors="replace")
        for c, n in con.execute("SELECT child, name FROM root_refs ORDER BY gen_max")
    }
    print(f"{'tree':>20}  {'kind':14} {'name':20} {'leaves':>8} {'nodes':>7}  generations")
    for owner, leaves, nodes, gmin, gmax in con.execute(
        f"SELECT owner, sum(level=0), sum(level>0), min(gen), max(gen) FROM nodes {where} "
        "GROUP BY owner ORDER BY count(*) DESC",
        params,
    ):
        t = u64(owner)
        print(
            f"{t:>20}  {od.tree_name(t):14} {names.get(t, ''):20} {leaves:>8} {nodes:>7}  "
            f"{gmin}-{gmax}"
        )
    return 0


def cmd_extract(args, cfg):
    from .extract import extract, pick_fsid

    with _device(args, cfg) as dev:
        con, path = _db(args, cfg)
        fsid = pick_fsid(con, args.fsid)
        nodesize = int(get_meta(con, "nodesize", "16384"))
        trees = {int(t) for t in args.trees.split(",")} if args.trees else None
        print(
            f"extracting fsid {fmt_uuid(fsid)} "
            f"(trees: {'all fs trees' if trees is None else sorted(trees)}) -> {path}",
            file=sys.stderr,
        )
        stats = extract(
            dev,
            con,
            fsid,
            nodesize,
            trees=trees,
            allow_bad=args.allow_bad_csum,
            superblocks=dev.superblocks(),
        )
        print("  ".join(f"{k} {v}" for k, v in stats.items()))
    return cmd_subvols(args, cfg)


def cmd_subvols(args, cfg):
    from .model import RescueFS

    con, _ = _db(args, cfg)
    with _device(args, cfg) as dev:
        fs = RescueFS(con, dev)
        print(f"chunks known: {fs.chunk_count}")
        print(f"{'path':32} {'inodes':>9}")
        for t, name, count in fs.trees():
            print(f"/{name}@{t:<30} {count:>9}")
    return 0


def cmd_ls(args, cfg):
    from .shell import Shell

    with _device(args, cfg) as dev:
        sh = Shell(_open_fs(args, cfg, dev))
        sh.onecmd(
            "ls " + ("-l " if args.long else "") + ("-a " if args.all else "") + _quote(args.path)
        )
    return 0


def cmd_shell(args, cfg):
    from .shell import Shell

    with _device(args, cfg) as dev:
        fs = _open_fs(args, cfg, dev)
        sh = Shell(fs)
        if args.path:
            sh.onecmd("cd " + _quote(args.path))
        try:
            import readline

            readline.set_completer_delims(" \t\n")
        except ImportError:
            pass
        try:
            sh.cmdloop()
        except KeyboardInterrupt:
            print()
    return 0


def _categories_from_args(args) -> frozenset[str]:
    from .model import CATEGORIES
    from .restore import DEFAULT_CATEGORIES

    cats = set(DEFAULT_CATEGORIES)
    for item in args.include or []:
        cats |= set(CATEGORIES) if item == "all" else {item}
    return frozenset(cats)


def _print_totals(totals: dict[str, tuple[int, int]]) -> None:
    from .model import CAT_HELP
    from .shell import fmt_size

    for cat, (n, size) in totals.items():
        print(f"  {cat:11} {n:>9} files {fmt_size(size):>9}   {CAT_HELP[cat]}")


def cmd_restore(args, cfg):
    from .restore import restore
    from .shell import fmt_size

    cats = _categories_from_args(args)
    with _device(args, cfg) as dev:
        fs = _open_fs(args, cfg, dev)
        chain = fs.resolve(args.path)
        if not chain:
            raise SystemExit("choose a path inside a subvolume, e.g. /_work@257/project")
        if not fs.masks_valid():
            print("note: not classified yet - checking files on the fly (slower)", file=sys.stderr)
        stats, report = restore(
            fs,
            chain[-1],
            Path(args.dest).expanduser(),
            categories=cats,
            overwrite=args.overwrite,
            include_stale=args.include_stale,
            best=not args.latest,
            patch=not args.no_patch,
        )
        print(
            f"wrote {stats.files} files ({fmt_size(stats.bytes)}), {stats.links} symlinks, "
            f"{stats.older_versions} from an older (better) version, "
            f"{stats.patched} reconstructed (older versions / git, as in best/); "
            f"skipped {stats.skipped_category} by category, {stats.skipped} already existing"
        )
        print(f"categories seen: {stats.by_category}  (written: {', '.join(sorted(cats))})")
        print(f"report: {report}")
    return 1 if stats.problems else 0


def cmd_review(args, cfg):
    import contextlib

    from .model import CATEGORIES, ROOT
    from .review import DEFAULT_REVIEW, review

    cats = tuple(args.include.split(",")) if args.include else DEFAULT_REVIEW
    if unknown := set(cats) - set(CATEGORIES):
        raise SystemExit(f"unknown categories: {', '.join(sorted(unknown))} (use {CATEGORIES})")
    counts: dict[str, int] = {}
    with _device(args, cfg) as dev, contextlib.ExitStack() as stack:
        fs = _open_fs(args, cfg, dev)
        if not fs.masks_valid():
            raise SystemExit("not classified (or classified with other rules) - run `classify`")
        chain = fs.resolve(args.path)
        if chain:
            starts = [(fs.chain_path(chain), chain[-1])]
        else:  # the root: every subvolume
            starts = [(f"/{name}", e) for name, e in fs.children(ROOT).items()]
        out = sys.stdout
        if args.output:
            out = stack.enter_context(open(args.output, "w", newline=""))
        for i, (rel, e) in enumerate(starts):
            found = review(fs, e, rel, out, cats, best=not args.latest, header=i == 0)
            for cat, n in found.items():
                counts[cat] = counts.get(cat, 0) + n
    summary = ", ".join(f"{n} {c}" for c, n in sorted(counts.items())) or "nothing to review"
    print(summary + (f" -> {args.output}" if args.output else ""), file=sys.stderr)
    return 0


def cmd_status(args, cfg):
    from .tools import status

    print(status(cfg, _db_path(args, cfg)))
    return 0


def cmd_umount(args, cfg):
    from .tools import our_mounts, umount

    points = args.mountpoint or our_mounts()
    if not points:
        print("nothing mounted")
        return 0
    failed = umount(points, lazy=args.lazy)
    for m in points:
        err = dict(failed).get(m)
        print(f"{m}: {'FAILED - ' + err if err else 'unmounted'}")
    if failed and not args.lazy:
        print(
            "a program is still using it (a shell cd'ed there, a file manager); close it or "
            "use --lazy (detaches now, finishes when it is no longer used)"
        )
    return 1 if failed else 0


def cmd_stop(args, cfg):
    from .tools import our_mounts, our_processes, stop

    mounts, procs = our_mounts(), our_processes()
    if not mounts and not procs:
        print("nothing running")
        return 0
    problems = stop(force=args.force)
    print(
        f"unmounted {len(mounts)}, stopped {len(procs)} commands (analyses resume on the next run)"
        if not problems
        else "\n".join(problems)
    )
    return 1 if problems else 0


def cmd_clean(args, cfg):
    from .tools import clean_items, fmt, remove, untouched

    items = clean_items(cfg, hashes=args.hashes)
    for i in items:
        print(f"  {fmt(i.size):>8}  {i.label:18} {i.path}")
    total = sum(i.size for i in items)
    other = untouched(cfg, items)
    if other:
        print("kept (not created by this tool, or your data):")
        for p in other:
            print(f"            {p}{'  (mount point)' if os.path.ismount(p) else ''}")
    if not items:
        print("nothing to clean")
        return 0
    if not args.yes:
        print(
            f"would free {fmt(total)}; run again with --yes to delete"
            + ("" if args.hashes else " (--hashes also removes the sector-hash cache)")
        )
        return 0
    skipped = remove(items)
    for s in skipped:
        print(f"skipped {s}")
    print(f"freed {fmt(total)}")
    return 0


def _db_path(args, cfg) -> Path:
    return Path(args.db) if args.db else cfg.db_path


def cmd_git_rescue(args, cfg):
    from .gitrescue import git_rescue

    with _device(args, cfg) as dev:
        fs = _open_fs(args, cfg, dev)
        if not fs.masks_valid():
            raise SystemExit("not classified (or classified with other rules) - run `classify`")
        dest = Path(args.dest).expanduser() if args.dest else None
        stats, report = git_rescue(fs, args.path, dest, scratch=cfg.tmp_dir)
    print(
        f"{stats.repos} repositories with lost/damaged files, {stats.files} files: "
        f"{stats.verified} verified identical, {stats.same_size} same size, "
        f"{stats.other} other version, {stats.object_lost} with their git object lost too, "
        f"{stats.untracked} not tracked; "
        f"sources {stats.by_source}"
        + (f"; wrote {stats.written} -> {dest} (report: {report})" if dest else ""),
        file=sys.stderr,
    )
    return 0


def cmd_classify(args, cfg):
    from .classify import classify
    from .pipeline import STATE_KEY

    with _device(args, cfg) as dev:
        fs = _open_fs(args, cfg, dev)
        trees = {int(t) for t in args.trees.split(",")} if args.trees else None
        match_dir = None if args.no_match or trees else _db_path(args, cfg).parent
        totals = classify(
            fs,
            trees=trees,
            quick=args.quick,
            match_dir=match_dir,
            scratch=cfg.tmp_dir,
            patch=not args.no_patch,
        )
        if get_meta(fs.con, "scan_done") == "1":
            fs.con.execute("INSERT OR REPLACE INTO meta VALUES (?, 'complete')", (STATE_KEY,))
            fs.con.commit()
    _print_totals(totals)
    return 0


def cmd_match(args, cfg):
    from .classify import categorise, print_match
    from .match import run_match

    with _device(args, cfg) as dev:
        fs = _open_fs(args, cfg, dev)
        if not fs.con.execute("SELECT 1 FROM extent_status LIMIT 1").fetchone():
            raise SystemExit("no checked extents yet - run `classify` first")
        print_match(run_match(fs, _db_path(args, cfg).parent))
        totals = categorise(fs, cfg.tmp_dir)
    _print_totals(totals)
    return 0


def cmd_analyze(args, cfg):
    from .model import RescueFS
    from .pipeline import analyze, prepare

    with _device(args, cfg) as dev:
        con, path = _db(args, cfg)
        fsid = bytes.fromhex(args.fsid.replace("-", "")) if args.fsid else None
        print(f"analyzing {dev.path} -> {path} (resumable; Ctrl-C to pause)", file=sys.stderr)
        params = prepare(dev, con, fsid=fsid, restart=args.restart)
        analyze(
            dev,
            con,
            params,
            exclude=_exclude(args, cfg),
            quick=args.quick,
            match_dir=None if args.no_match else path.parent,
            scratch=cfg.tmp_dir,
            patch=not args.no_patch,
        )
        _print_totals(RescueFS(con, dev, exclude=_exclude(args, cfg)).category_totals())
    return 0


def cmd_mount(args, cfg):
    import threading

    from .db import set_meta
    from .fusefs import mount
    from .model import RescueFS
    from .pipeline import (
        STATE_KEY,
        STATUS_KEY,
        analysis_state,
        analyze,
        classification_outdated,
        prepare,
    )

    dev = _device(args, cfg)
    con, db_path = _db(args, cfg)
    exclude = _exclude(args, cfg)
    if (
        not args.no_analyze
        and analysis_state(con) == "complete"
        and (classification_outdated(con, exclude) or get_meta(con, "patch_serial") is None)
    ):  # new rules (after an upgrade), another exclude list, no copy search yet: classify
        # again, live
        set_meta(con, STATE_KEY, "classifying")
        con.commit()
    if not args.no_analyze and analysis_state(con) != "complete":
        params = prepare(dev, con)
        log_path = cfg.tmp_dir / "analyze.log"

        def worker() -> None:
            wcon = connect(db_path)
            with Device(dev.path) as wdev, log_path.open("a") as log:
                try:
                    analyze(
                        wdev,
                        wcon,
                        params,
                        exclude=exclude,
                        echo=False,
                        match_dir=db_path.parent,
                        scratch=cfg.tmp_dir,
                    )
                except Exception as err:
                    set_meta(wcon, STATUS_KEY, f"ERROR: {err!r} (see {log_path})")
                    wcon.commit()
                    print(repr(err), file=log)

        threading.Thread(target=worker, name="analyze", daemon=True).start()
        print(f"analysis running in the background (log: {log_path})", file=sys.stderr)

    def reload():
        if get_meta(con, "fsid") is None:
            return None
        return RescueFS(con, dev, exclude=exclude, at_gen=args.at_gen)

    layered = args.at_gen is None and not args.flat
    print(
        f"mounted read-only at {args.mountpoint} - see README.txt there. "
        f"Ctrl-C or `fusermount3 -u {args.mountpoint}` to unmount.",
        file=sys.stderr,
    )
    mount(
        reload(),
        args.mountpoint,
        foreground=True,
        layered=layered,
        con=con,
        reload=reload,
        refresh=args.refresh,
        show_unreadable=args.show_unreadable,
    )
    return 0


def _exclude(args, cfg) -> list[str]:
    return [] if args.no_exclude else list(cfg.exclude) + (args.exclude or [])


def _quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


# ---------------------------------------------------------------------------- parser


NO_PATCH_HELP = "do not reconstruct damaged/lost files (older versions, git) for best/"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mbkn-btrfs-rescue",
        description="Read-only btrfs recovery: scan a device for tree blocks (all "
        "generations), index them, then browse, mount or restore files.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", help="config file (TOML)")
    p.add_argument("-d", "--device", help="device or image file (overrides config)")
    p.add_argument("--db", help="SQLite index path (default: <cache_dir>/<db_name>)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def view_opts(sp):
        sp.add_argument("--at-gen", type=int, help="view state as of this generation")
        sp.add_argument(
            "-x", "--exclude", action="append", help="additional name to hide (repeatable)"
        )
        sp.add_argument("--no-exclude", action="store_true", help="ignore config `exclude`")

    s = sub.add_parser("info", help="show superblocks and backup roots")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("scan", help="pass 1: sweep the device for tree blocks")
    s.add_argument(
        "--fsid", default="any", help="only keep blocks of this filesystem UUID (default: any)"
    )
    s.add_argument("--start", default="0", help="start offset, e.g. 0, 10G")
    s.add_argument("--end", help="end offset (default: device end)")
    s.add_argument("--nodesize", type=parse_size, help="override node size (default: superblock)")
    s.add_argument("--sectorsize", type=parse_size, help="override sector size")
    s.add_argument(
        "--csum-type",
        type=int,
        choices=[0, 1, 2, 3],
        help="0 crc32c, 1 xxhash64, 2 sha256, 3 blake2b (default: superblock)",
    )
    s.add_argument("--chunk-mb", type=int, default=64, help="read size per step (MiB)")
    s.add_argument("--resume", action="store_true", help="continue an interrupted scan")
    s.add_argument("--force", action="store_true", help="discard existing scan data")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("fsids", help="list filesystem UUIDs found by the scan")
    s.set_defaults(func=cmd_fsids)

    s = sub.add_parser("trees", help="list trees (owners) found by the scan")
    s.set_defaults(func=cmd_trees)

    s = sub.add_parser("extract", help="pass 2: parse leaves into the browsable index")
    s.add_argument("--fsid", help="filesystem UUID to extract (default: most blocks)")
    s.add_argument("--trees", help="comma-separated fs tree ids (default: all)")
    s.add_argument(
        "--allow-bad-csum",
        action="store_true",
        help="also use leaves whose checksum does not match",
    )
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("subvols", help="list recovered subvolumes / fs trees")
    s.set_defaults(func=cmd_subvols)

    s = sub.add_parser("ls", help="list a path")
    s.add_argument("path", nargs="?", default="/")
    s.add_argument("-l", "--long", action="store_true")
    s.add_argument("-a", "--all", action="store_true", help="include stale names")
    view_opts(s)
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("shell", help="interactive browser")
    s.add_argument("path", nargs="?", help="start directory")
    view_opts(s)
    s.set_defaults(func=cmd_shell)

    s = sub.add_parser("analyze", help="scan + extract + classify in one resumable run")
    s.add_argument("--fsid", help="filesystem UUID (default: from the superblock)")
    s.add_argument("--quick", action="store_true", help="sample 3 sectors per extent")
    s.add_argument("--restart", action="store_true", help="discard previous progress")
    s.add_argument("--no-match", action="store_true", help="skip recovery from copies")
    s.add_argument("--no-patch", action="store_true", help=NO_PATCH_HELP)
    view_opts(s)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("classify", help="pass 3: verify data and categorise every file")
    s.add_argument("--trees", help="comma-separated fs tree ids to check (default: all)")
    s.add_argument("--quick", action="store_true", help="sample 3 sectors per extent")
    s.add_argument("--no-match", action="store_true", help="skip recovery from copies")
    s.add_argument("--no-patch", action="store_true", help=NO_PATCH_HELP)
    view_opts(s)
    s.set_defaults(func=cmd_classify)

    s = sub.add_parser("status", help="mounts, running commands, progress, disk usage")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("umount", help="unmount this tool's FUSE mounts (default: all)")
    s.add_argument("mountpoint", nargs="*")
    s.add_argument("--lazy", action="store_true", help="detach even if in use")
    s.set_defaults(func=cmd_umount)

    s = sub.add_parser("stop", help="unmount everything and pause running analyses")
    s.add_argument("--force", action="store_true", help="SIGTERM instead of SIGINT")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("clean", help="remove test images, scratch dirs, logs (dry run first)")
    s.add_argument("--yes", action="store_true", help="really delete")
    s.add_argument("--hashes", action="store_true", help="also the sector-hash cache")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser(
        "git-rescue", help="recover lost/damaged files of git work trees from .git (TSV)"
    )
    s.add_argument("path", nargs="?", default="/", help="directory to look for repositories in")
    s.add_argument("--dest", help="write the recovered files here (default: report only)")
    view_opts(s)
    s.set_defaults(func=cmd_git_rescue)

    s = sub.add_parser(
        "review", help="list unverified and damaged files with their bad byte ranges (TSV)"
    )
    s.add_argument("path", nargs="?", default="/", help="directory or file (default: all)")
    s.add_argument("-o", "--output", help="write the TSV here instead of stdout")
    s.add_argument(
        "--include",
        help="comma-separated categories to list (default: unverified,damaged)",
    )
    s.add_argument("--latest", action="store_true", help="judge the newest versions, not best")
    view_opts(s)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser(
        "match", help="pass 4: recover bad sectors from identical copies on the device"
    )
    view_opts(s)
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("restore", help="copy a file or directory out")
    s.add_argument("path", help="path in the recovered namespace, e.g. /_work@257/proj")
    s.add_argument("dest", help="destination directory (created if missing)")
    s.add_argument(
        "--include",
        action="append",
        choices=["damaged", "lost", "all"],
        help="also write files of this category (default: intact + unverified)",
    )
    s.add_argument("--overwrite", action="store_true")
    s.add_argument(
        "--include-stale", action="store_true", help="also restore old names of renamed/moved files"
    )
    s.add_argument(
        "--latest",
        action="store_true",
        help="always write the newest version (default: best version, as in the mount's best/)",
    )
    s.add_argument(
        "--no-patch",
        action="store_true",
        help="write damaged/lost files' own content, not the reconstructions best/ shows",
    )
    view_opts(s)
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("mount", help="read-only FUSE mount, live while analysis runs")
    s.add_argument("mountpoint")
    s.add_argument("--flat", action="store_true", help="mount only the `all` view at the root")
    s.add_argument(
        "--no-analyze",
        action="store_true",
        help="do not start the background analysis when the index is incomplete",
    )
    s.add_argument(
        "--refresh", type=float, default=30.0, help="seconds between index reloads (default 30)"
    )
    s.add_argument(
        "--show-unreadable",
        action="store_true",
        help="also list files without recoverable data (all zeros / lost) in all/ and history/",
    )
    view_opts(s)
    s.set_defaults(func=cmd_mount)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    cfg.prepare_dirs()
    try:
        return args.func(args, cfg) or 0
    except DeviceError as err:
        print(err, file=sys.stderr)
        return 2
    except FileNotFoundError as err:
        print(f"not found: {err}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted - run the same command again to continue", file=sys.stderr)
        return 130
