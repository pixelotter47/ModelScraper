"""Private runtime state owned by one checkout of the public edition.

Browser cookies and cleanup queues must never be inherited from a personal
ModelScraper installation or another checkout. The machine-wide VPN mutex
and recovery journal remain shared because they protect a shared resource.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


RUNTIME_NAMESPACE = "ModelScraperPublic"


def checkout_root() -> Path:
    return Path(__file__).resolve().parent


def public_state_root(workspace=None) -> Path:
    """Return a deterministic isolated path without creating any files."""
    workspace = Path(workspace).resolve() if workspace is not None else checkout_root()
    normalized = os.path.normcase(str(workspace))
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CACHE_HOME")
        or tempfile.gettempdir()
    )
    return Path(base, RUNTIME_NAMESPACE, key)
