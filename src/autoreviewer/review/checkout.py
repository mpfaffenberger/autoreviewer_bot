"""Unpack a repository snapshot (GitHub tarball) for the reviewers' read-only file tools.

The tarball is attacker-controlled: a PR can add any file. Extraction uses tarfile's "data" filter
(no absolute paths, no "..", no links leaving the tree, no devices) and size caps checked before
anything is written, so a crafted archive can neither escape the directory nor fill the disk.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 50_000


class CheckoutError(RuntimeError):
    """The snapshot can't be used: too big, malformed, or unsafe."""


def extract(tarball: bytes, destination: Path) -> Path:
    """Extract a GitHub tarball into `destination`; return its single top-level directory."""
    if len(tarball) > MAX_DOWNLOAD_BYTES:
        raise CheckoutError(
            f"repository archive is {len(tarball) // 2**20} MiB; the limit is {MAX_DOWNLOAD_BYTES // 2**20} MiB"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise CheckoutError(f"repository has {len(members)} files; the limit is {MAX_MEMBERS}")
            if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED_BYTES:
                raise CheckoutError(f"repository unpacks to more than {MAX_UNPACKED_BYTES // 2**20} MiB")
            archive.extractall(destination, filter="data")
    except tarfile.TarError as error:  # includes the data filter's refusals (OutsideDestinationError, ...)
        raise CheckoutError(f"unusable repository archive: {error}") from error
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise CheckoutError(f"expected one top-level directory in the archive, found {len(roots)}")
    return roots[0]


async def extract_async(tarball: bytes, destination: Path) -> Path:
    return await asyncio.to_thread(extract, tarball, destination)
