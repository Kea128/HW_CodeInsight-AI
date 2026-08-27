"""Runtime configuration shared by the frozen desktop analysis engine."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_bundled_tiktoken_cache() -> Path | None:
    """Point tiktoken at the verified encoding shipped inside a frozen build."""
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    cache_dir = bundle_root / "tiktoken_cache"
    if not cache_dir.is_dir():
        return None
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
    return cache_dir
