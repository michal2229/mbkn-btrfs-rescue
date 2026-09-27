"""btrfs checksum algorithms (tree blocks and data sectors)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import crc32c
import xxhash

from .ondisk import CSUM_BLAKE2, CSUM_CRC32C, CSUM_SHA256, CSUM_SIZES, CSUM_XXHASH

CSUM_NAMES = {
    CSUM_CRC32C: "crc32c",
    CSUM_XXHASH: "xxhash64",
    CSUM_SHA256: "sha256",
    CSUM_BLAKE2: "blake2b",
}


def csum_function(csum_type: int) -> Callable[[bytes], bytes]:
    """Return f(data) -> raw checksum bytes as stored on disk (not padded)."""
    match csum_type:
        case 0:
            return lambda d: crc32c.crc32c(d).to_bytes(4, "little")
        case 1:
            return lambda d: xxhash.xxh64_intdigest(d).to_bytes(8, "little")
        case 2:
            return lambda d: hashlib.sha256(d).digest()
        case 3:
            return lambda d: hashlib.blake2b(d, digest_size=32).digest()
    raise ValueError(f"unknown checksum type {csum_type}")


class BlockVerifier:
    def __init__(self, csum_type: int, nodesize: int):
        self.fn = csum_function(csum_type)
        self.size = CSUM_SIZES[csum_type]
        self.nodesize = nodesize

    def __call__(self, block: bytes | memoryview) -> bool:
        return self.fn(block[32 : self.nodesize]) == bytes(block[: self.size])
