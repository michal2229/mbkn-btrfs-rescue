"""Recover lost files of git work trees from their repository's objects.

A source file whose data was discarded is often still in its repository: staged in the index,
in the latest stash, or committed. For every git work tree below a path, the readable part of
its `.git` directory is restored to a scratch directory, and for each lost or damaged file the
blobs recorded for its path are read with the `git` command:

    index  (staged: `git add`), stash (refs/stash, the work tree when stashed), HEAD

A blob is checked against the lost file's recorded btrfs checksums: when every comparable
sector matches, the blob *is* the lost content ("verified"). Compressed extents are checksummed
after compression, so they cannot be compared ("0/0" sectors); then the report says whether the
size matches the lost version, so older committed content is easy to tell apart. Tracked files
whose blob was lost too (loose objects are files like any other) are reported as "object lost".

Safety: the restored repository never runs anything. Its config is replaced by a minimal one
(object format only), hooks are not restored, and git runs with system/global config disabled.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import ondisk as od
from .model import (
    CAT_BITS,
    CAT_DAMAGED,
    CAT_INTACT,
    CAT_LOST,
    CAT_UNVERIFIED,
    Entry,
    RescueFS,
)
from .restore import restore

GIT = shutil.which("git")
SOURCES = ("index", "stash", "HEAD")
WANTED = frozenset({CAT_LOST, CAT_DAMAGED})


@dataclass
class Candidate:
    source: str
    sha: str
    data: bytes = b""
    matched: int = 0
    compared: int = 0

    @property
    def verified(self) -> bool:
        return self.compared > 0 and self.matched == self.compared


@dataclass
class GitStats:
    repos: int = 0
    repos_unreadable: int = 0
    files: int = 0  # lost/damaged files looked at
    verified: int = 0
    same_size: int = 0
    other: int = 0
    untracked: int = 0  # the path is not in the index, stash or HEAD
    object_lost: int = 0  # tracked, but the blob is unreadable (lost with the file)
    written: int = 0
    by_source: dict[str, int] = field(default_factory=dict)


def find_repos(fs: RescueFS, under: str = "/") -> list[tuple[str, Entry, Entry]]:
    """[(work tree path, work tree entry, .git entry)] below `under`."""
    prefix = under.rstrip("/") + "/"
    out = {}
    for tree, d in fs.con.execute(
        "SELECT DISTINCT tree, dir FROM dirents WHERE name=? AND child_kind=1", (b".git",)
    ):
        path = fs.path_of(tree, d)
        if path is None or not (path + "/").startswith(prefix):
            continue
        try:
            chain = fs.resolve(path)
        except FileNotFoundError:  # inside an excluded directory
            continue
        root = chain[-1]
        git = fs.children(root.node).get(".git")
        if git is None or not git.is_dir or git.stale:
            continue  # .git file (linked work tree / submodule): objects live elsewhere
        out[path] = (path, root, git)
    return [out[p] for p in sorted(out)]


def _git(git_dir: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(git_dir),
        "GIT_DIR": str(git_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    assert GIT is not None
    return subprocess.run(
        [GIT, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args],
        input=stdin,
        capture_output=True,
        env=env,
        check=False,
    )


def _neutralise(git_dir: Path) -> None:
    """Replace the restored config by a minimal one; drop hooks."""
    git_dir.mkdir(parents=True, exist_ok=True)  # nothing readable: restore left no .git
    fmt = "sha1"
    cfg = git_dir / "config"
    with contextlib.suppress(OSError):
        m = re.search(r"objectformat\s*=\s*(\w+)", cfg.read_text(errors="replace"), re.I)
        if m:
            fmt = m.group(1).lower()
    text = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
    if fmt != "sha1":
        text = (
            "[core]\n\trepositoryformatversion = 1\n\tbare = true\n"
            f"[extensions]\n\tobjectformat = {fmt}\n"
        )
    cfg.write_text(text)
    shutil.rmtree(git_dir / "hooks", ignore_errors=True)


def _listing(git_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """{path: [(source, blob id)]} from index, stash and HEAD (regular files only)."""
    out: dict[str, list[tuple[str, str]]] = {}

    def add(source: str, entries: list[bytes], sha_at: int) -> None:
        for rec in entries:
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            parts = meta.split()
            if len(parts) <= sha_at or not parts[0].startswith(b"100"):
                continue  # not a regular file (symlink 120000, gitlink 160000)
            p = os.fsdecode(path)
            lst = out.setdefault(p, [])
            sha = parts[sha_at].decode()
            if all(s != sha for _src, s in lst):
                lst.append((source, sha))

    r = _git(git_dir, "ls-files", "-s", "-z")
    if r.returncode == 0:
        add("index", r.stdout.split(b"\0"), 1)
    for source, ref in (("stash", "refs/stash"), ("HEAD", "HEAD")):
        if _git(git_dir, "rev-parse", "-q", "--verify", f"{ref}^{{tree}}").returncode:
            continue
        r = _git(git_dir, "ls-tree", "-r", "-z", ref)
        if r.returncode == 0:
            add(source, r.stdout.split(b"\0"), 2)
    return out


def _blobs(git_dir: Path, shas: list[str]) -> dict[str, bytes]:
    """Read blobs; only those whose content hashes to their id are returned."""
    if not shas:
        return {}
    r = _git(git_dir, "cat-file", "--batch", stdin="".join(f"{s}\n" for s in shas).encode())
    out: dict[str, bytes] = {}
    buf, pos = r.stdout, 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].split()
        pos = nl + 1
        if len(header) != 3:  # "<sha> missing"
            continue
        sha, kind, size = header[0].decode(), header[1], int(header[2])
        data = buf[pos : pos + size]
        pos += size + 1
        algo = hashlib.sha256 if len(sha) == 64 else hashlib.sha1
        if kind == b"blob" and algo(b"blob %d\0" % size + data).hexdigest() == sha:
            out[sha] = data
    return out


@dataclass
class GitMatch:
    """What a repository holds for one lost/damaged work-tree file."""

    repo: str  # work tree path
    path: str  # namespace path
    entry: Entry
    category: str  # best category of the file itself
    lost_size: int
    best: Candidate | None  # readable blob, most likely to be the lost content
    check: str  # verified | same size | size differs | object lost: ... | not tracked


def git_matches(
    fs: RescueFS,
    under: str = "/",
    *,
    categories: frozenset[str] = WANTED,
    scratch: Path | None = None,
    progress: bool = True,
) -> Iterator[GitMatch]:
    """For every lost/damaged file in git work trees below `under`, its best blob."""
    if GIT is None:
        return
    repos = find_repos(fs, under)
    bits = CAT_BITS[CAT_DAMAGED] | CAT_BITS[CAT_LOST]
    for n, (wt_path, root, git_entry) in enumerate(repos, 1):
        if fs.masks_valid() and not fs.entry_mask(root) & bits:
            continue  # nothing damaged or lost in this work tree
        files = _worktree_files(fs, root, categories)
        if not files:
            continue
        if progress:
            print(
                f"\r  git: repository {n}/{len(repos)} ({len(files)} files) ",
                end="",
                file=sys.stderr,
                flush=True,
            )
        with tempfile.TemporaryDirectory(dir=scratch, prefix="git-rescue-") as tmp:
            restore(
                fs,
                git_entry,
                Path(tmp),
                categories=frozenset({CAT_INTACT, CAT_UNVERIFIED}),
                patch=False,
                progress=False,
            )
            git_dir = Path(tmp) / ".git"
            _neutralise(git_dir)
            listing = _listing(git_dir)
            wanted = {rel: listing.get(rel, []) for rel in files}
            blobs = _blobs(git_dir, sorted({s for c in wanted.values() for _src, s in c}))
        for rel, (e, cat) in files.items():
            yield _match(fs, wt_path, rel, e, cat, wanted[rel], blobs)
    if progress and repos:
        print(file=sys.stderr)


def _match(fs: RescueFS, repo: str, rel: str, e: Entry, cat: str, cands, blobs) -> GitMatch:
    t, i = e.node.tree, e.node.ino
    lost_size = fs.layout(t, i).size
    best: Candidate | None = None
    for source, sha in cands:
        data = blobs.get(sha)
        if data is None:
            continue
        c = Candidate(source, sha, data, *fs.compare_content(t, i, data))
        key = (c.verified, len(data) == lost_size)
        if best is None or key > (best.verified, len(best.data) == lost_size):
            best = c
    if best is None:
        check = (
            "object lost: " + ",".join(sorted({src for src, _sha in cands}))
            if cands
            else "not tracked"
        )
    elif best.verified:
        check = "verified"
    elif len(best.data) == lost_size:
        check = "same size"
    else:
        check = "size differs"
    return GitMatch(repo, f"{repo}/{rel}", e, cat, lost_size, best, check)


def git_rescue(
    fs: RescueFS,
    under: str,
    dest: Path | None,
    *,
    categories: frozenset[str] = WANTED,
    scratch: Path | None = None,
    progress: bool = True,
) -> tuple[GitStats, Path | None]:
    """Report (and with `dest`: write) repository content for lost/damaged work tree files."""
    if GIT is None:
        raise SystemExit("git is not installed")
    stats = GitStats()
    report_path = None
    if dest is not None:
        dest.mkdir(parents=True, exist_ok=True)
        report_path = dest / f".mbkn-git-rescue-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    repos: set[str] = set()
    with contextlib.ExitStack() as stack:
        fh = stack.enter_context(report_path.open("w", newline="")) if report_path else sys.stdout
        rep = csv.writer(fh, delimiter="\t", lineterminator="\n")
        rep.writerow(
            ["path", "category", "source", "check", "sectors_matching", "size", "lost_size",
             "action"]
        )  # fmt: skip
        for m in git_matches(fs, under, categories=categories, scratch=scratch, progress=progress):
            stats.files += 1
            repos.add(m.repo)
            _record(fs, m, dest, rep, stats)
    stats.repos = len(repos)
    return stats, report_path


def _worktree_files(
    fs: RescueFS, root: Entry, categories: frozenset[str]
) -> dict[str, tuple[Entry, str]]:
    """{path relative to the work tree: (entry, best category)} of files to recover."""
    out = {}
    for rel, e in fs.walk(root, prefix="."):
        if e.is_dir or e.ftype != od.FT_REG:
            continue
        rel = rel[2:]
        if rel.startswith(".git/") or "/.git/" in rel:
            continue
        cat = fs.best_version(e.node.tree, e.node.ino)[1]
        if cat in categories:
            out[rel] = (e, cat)
    return out


def _record(fs: RescueFS, m: GitMatch, dest: Path | None, rep, stats: GitStats) -> None:
    best = m.best
    if best is None:
        if m.check == "not tracked":
            stats.untracked += 1
        else:
            stats.object_lost += 1
        rep.writerow([m.path, m.category, "", m.check, "", "", m.lost_size, "none"])
        return
    if m.check == "verified":
        stats.verified += 1
    elif m.check == "same size":
        stats.same_size += 1
    else:
        stats.other += 1
    stats.by_source[best.source] = stats.by_source.get(best.source, 0) + 1
    action = "report"
    if dest is not None:
        target = dest / m.path.lstrip("/")
        if target.exists():
            action = "exists"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(best.data)
            info = fs.inode(m.entry.node.tree, m.entry.node.ino)
            if info and best.verified:
                os.utime(target, (info.atime, info.mtime))
            stats.written += 1
            action = "written"
    rep.writerow(
        [m.path, m.category, best.source, m.check, f"{best.matched}/{best.compared}",
         len(best.data), m.lost_size, action]
    )  # fmt: skip
