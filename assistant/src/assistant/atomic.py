"""Atomic text-file replacement, shared by every state writer.

Write to a sibling temp file, then ``os.replace`` — a crash mid-write leaves
the previous file intact instead of a truncated one.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, overwrite: bool = True) -> None:
    """Publish a complete file; exclusive creates raise FileExistsError on collision."""
    mode = None
    if overwrite:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            tmp.chmod(mode)
        if overwrite:
            os.replace(tmp, path)
        else:
            # A hard link publishes the completed file without replacing a
            # destination created since the caller checked for its existence.
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
