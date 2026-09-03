"""Atomic text-file replacement, shared by every state writer.

Write to a sibling temp file, then ``os.replace`` — a crash mid-write leaves
the previous file intact instead of a truncated one.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
