"""Atomic single-file writes with durable temporary contents.

The temporary file lives alongside its destination so replacement stays on the
same filesystem. A successful return guarantees a flushed, fsynced file and an
atomic replacement; directory-entry durability after power loss depends on the
operating system and filesystem. Concurrent writers are last-replacement-wins.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Replace a file atomically, preserving the old file on pre-replace errors."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        # After replacement the temporary name no longer exists. On any error,
        # remove only this invocation's file, never a concurrent writer's file.
        Path(temporary).unlink(missing_ok=True)


def atomic_write_text(
    path: str | os.PathLike[str], text: str, encoding: str = "utf-8"
) -> None:
    """Encode text before replacing its destination atomically."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    encoding: str = "utf-8",
    ensure_ascii: bool = False,
    indent: int | str | None = 2,
    **kwargs: Any,
) -> None:
    """Serialize JSON before writing; additional options go to ``json.dumps``."""
    atomic_write_text(
        path, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent, **kwargs), encoding
    )
