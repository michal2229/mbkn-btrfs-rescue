"""Strictly read-only access to the block device or image file."""

from __future__ import annotations

import os
import stat

from .ondisk import SUPER_OFFSETS, Superblock, parse_superblock

ACCESS_HINT = (
    "Cannot read {path}: {err}.\n"
    "Grant temporary read-only access with:  scripts/device-access.sh grant {path}"
)


class DeviceError(RuntimeError):
    pass


class Device:
    """A read-only view of a device. Never opened with write flags."""

    def __init__(self, path: str):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except PermissionError as err:
            raise DeviceError(ACCESS_HINT.format(path=path, err=err.strerror)) from err
        except FileNotFoundError as err:
            raise DeviceError(f"{path}: no such device or file") from err
        self.size = os.lseek(self.fd, 0, os.SEEK_END)
        self.is_block = stat.S_ISBLK(os.fstat(self.fd).st_mode)

    def pread(self, length: int, offset: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            chunk = os.pread(self.fd, length - len(out), offset + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def readinto(self, buf: bytearray, offset: int) -> int:
        """Fill `buf` from `offset` (fewer bytes only at the end of the device)."""
        view = memoryview(buf)
        done = 0
        while done < len(buf):
            n = os.preadv(self.fd, [view[done:]], offset + done)
            if n <= 0:
                break
            done += n
        return done

    def advise_sequential(self) -> None:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)

    def drop_cache(self, offset: int, length: int) -> None:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self.fd, offset, length, os.POSIX_FADV_DONTNEED)

    def superblocks(self) -> list[Superblock]:
        out = []
        for off in SUPER_OFFSETS:
            if off + 4096 <= self.size:
                out.append(parse_superblock(self.pread(4096, off), off))
        return out

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
