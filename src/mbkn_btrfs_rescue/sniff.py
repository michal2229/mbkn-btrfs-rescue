"""Does a file's beginning look like its type? Used only when no checksum can decide.

Discarded (TRIMmed) blocks under LUKS read back as random bytes, and a file whose data has no
checksum on record cannot be verified. For known types, the first bytes still tell a lot: a
`.png` must start with the PNG signature, a `.py` must be text. Unknown types are not judged.
"""

from __future__ import annotations

import os
from collections.abc import Callable


def _starts(*magics: bytes) -> Callable[[bytes], bool]:
    return lambda head: any(head.startswith(m) for m in magics)


def _riff(kind: bytes) -> Callable[[bytes], bool]:
    return lambda head: head[:4] == b"RIFF" and head[8:12] == kind


def _ftyp(head: bytes) -> bool:
    return head[4:8] == b"ftyp"


def _mp3(head: bytes) -> bool:
    return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


def _pyc(head: bytes) -> bool:
    return head[2:4] == b"\r\n"


def _text(head: bytes) -> bool:
    if b"\0" in head:
        return False
    for cut in range(4):  # the sample may end inside a multi-byte character
        try:
            head[: len(head) - cut].decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


CHECKS: dict[str, Callable[[bytes], bool]] = {
    ".png": _starts(b"\x89PNG\r\n\x1a\n"),
    ".jpg": _starts(b"\xff\xd8\xff"),
    ".jpeg": _starts(b"\xff\xd8\xff"),
    ".gif": _starts(b"GIF87a", b"GIF89a"),
    ".webp": _riff(b"WEBP"),
    ".wav": _riff(b"WAVE"),
    ".avi": _riff(b"AVI "),
    ".mkv": _starts(b"\x1a\x45\xdf\xa3"),
    ".webm": _starts(b"\x1a\x45\xdf\xa3"),
    ".mp4": _ftyp,
    ".m4a": _ftyp,
    ".mov": _ftyp,
    ".heic": _ftyp,
    ".mp3": _mp3,
    ".flac": _starts(b"fLaC"),
    ".ogg": _starts(b"OggS"),
    ".opus": _starts(b"OggS"),
    ".exr": _starts(b"\x76\x2f\x31\x01"),
    ".pdf": _starts(b"%PDF"),
    ".zip": _starts(b"PK"),
    ".whl": _starts(b"PK"),
    ".jar": _starts(b"PK"),
    ".docx": _starts(b"PK"),
    ".xlsx": _starts(b"PK"),
    ".odt": _starts(b"PK"),
    ".gz": _starts(b"\x1f\x8b"),
    ".tgz": _starts(b"\x1f\x8b"),
    ".zst": _starts(b"\x28\xb5\x2f\xfd"),
    ".xz": _starts(b"\xfd7zXZ\x00"),
    ".bz2": _starts(b"BZh"),
    ".7z": _starts(b"7z\xbc\xaf\x27\x1c"),
    ".sqlite": _starts(b"SQLite format 3\x00"),
    ".sqlite3": _starts(b"SQLite format 3\x00"),
    ".pack": _starts(b"PACK"),  # git pack
    ".idx": _starts(b"\xfftOc"),  # git pack index v2
    ".pyc": _pyc,
}
TEXT = {
    ".md", ".txt", ".rst", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".jsonl", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".lock",
    ".html", ".htm", ".css", ".scss", ".svg", ".xml", ".csv", ".tsv", ".sql", ".sh", ".bash",
    ".zsh", ".fish", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt", ".rb",
    ".php", ".lua", ".pl", ".r", ".swift", ".cs", ".vue", ".svelte", ".tex", ".log",
    ".gitignore", ".dockerfile", ".mk", ".cmake", ".gradle", ".properties",
}  # fmt: skip
for _ext in TEXT:
    CHECKS[_ext] = _text

SNIFF_BYTES = 4096


def plausible(name: str, head: bytes) -> bool | None:
    """True/False if `head` (the file's first bytes) matches/contradicts the type implied by
    `name`; None when the type is unknown or there is nothing to look at."""
    if not head:
        return None
    ext = os.path.splitext(name)[1].lower() or (name.lower() if name.startswith(".") else "")
    check = CHECKS.get(ext)
    return None if check is None else check(head)
