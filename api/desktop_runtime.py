"""Runtime configuration shared by the frozen desktop analysis engine."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

TIKTOKEN_CACHE_KEY = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
TIKTOKEN_CACHE_SHA256 = (
    "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
)


def configure_bundled_tiktoken_cache() -> Path | None:
    """Point tiktoken at the verified encoding shipped inside a frozen build."""
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    cache_dir = bundle_root / "tiktoken_cache"
    cache_file = cache_dir / TIKTOKEN_CACHE_KEY
    if not cache_file.is_file():
        raise RuntimeError(f"Bundled tiktoken encoding is missing: {cache_file}")
    digest = hashlib.sha256(cache_file.read_bytes()).hexdigest()
    if digest != TIKTOKEN_CACHE_SHA256:
        raise RuntimeError(
            "Bundled tiktoken encoding failed integrity validation: "
            f"expected {TIKTOKEN_CACHE_SHA256}, got {digest}"
        )
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
    return cache_dir
