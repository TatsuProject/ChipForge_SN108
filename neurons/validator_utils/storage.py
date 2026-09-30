#!/usr/bin/env python3
"""
Crash-safe persistence for validator state.

- data_dir(): where everything that must survive a restart lives
  (CHIPFORGE_DATA_DIR, default: the current directory, i.e. the old behaviour).
- atomic_write_json(): write to a temp file in the same directory, fsync, then
  os.replace(). A crash mid-write leaves the previous file intact.
- load_json(): read a JSON file; a file that can't be parsed is moved aside
  (never silently overwritten) and None is returned.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def data_dir() -> Path:
    path = Path(os.getenv("CHIPFORGE_DATA_DIR", ".")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_path(name: str) -> Path:
    """Path of a state file inside the data directory (absolute names are kept as-is)."""
    p = Path(name)
    return p if p.is_absolute() else data_dir() / p


def atomic_write_json(path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path) -> Optional[Any]:
    """Parsed JSON, or None if the file doesn't exist or is unreadable (then moved aside)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, corrupt)
        except OSError:
            corrupt = path
        logger.error(f"STATE FILE UNREADABLE: {path} ({e}). Moved to {corrupt}; starting from defaults.")
        return None
