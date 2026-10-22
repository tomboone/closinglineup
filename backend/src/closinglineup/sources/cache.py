"""The raw cache: every NBA.com response as received, write-once, under `DATA_DIR/raw` (spec §5).

It is the contract between fetching and parsing (ADR-0002): parsers, and pbpstats, read only from
here, so a parse can be re-run from disk without the network.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from closinglineup.clock import Clock, SystemClock

SUFFIX = ".json"
META_SUFFIX = ".meta.json"


class CacheMeta(TypedDict):
    """The `.meta.json` sidecar beside each cached response."""

    fetched_at: str
    url: str
    status: int
    params: dict[str, str]


class RawCache:
    """`<data_dir>/raw/<source>/<endpoint>/<key>.json`, plus a `<key>.meta.json` sidecar.

    `put` writes each file atomically, sidecar first, so a response file that exists is always
    complete and always has its sidecar, and its presence alone makes the key a cache hit.
    """

    def __init__(self, data_dir: Path, clock: Clock | None = None) -> None:
        self.root = data_dir / "raw"
        self.clock = clock or SystemClock()

    def path(self, source: str, endpoint: str, key: str) -> Path:
        return self.root / source / endpoint / f"{key}{SUFFIX}"

    def exists(self, source: str, endpoint: str, key: str) -> bool:
        return self.path(source, endpoint, key).is_file()

    def get(self, source: str, endpoint: str, key: str) -> bytes | None:
        path = self.path(source, endpoint, key)
        return path.read_bytes() if path.is_file() else None

    def put(
        self,
        source: str,
        endpoint: str,
        key: str,
        content: bytes,
        *,
        url: str,
        status: int,
        params: Mapping[str, str] | None = None,
        force: bool = False,
    ) -> bool:
        """Write `content` unless the key is already cached; `force` overwrites. True if written."""
        path = self.path(source, endpoint, key)
        if path.is_file() and not force:
            return False
        meta: CacheMeta = {
            "fetched_at": self.clock.now().isoformat(),
            "url": url,
            "status": status,
            "params": dict(params or {}),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path.with_name(f"{key}{META_SUFFIX}"), json.dumps(meta, indent=2).encode())
        _write_atomic(path, content)
        return True

    def list(self, source: str, endpoint: str) -> list[str]:
        """The cached keys for one endpoint, sorted."""
        directory = self.root / source / endpoint
        if not directory.is_dir():
            return []
        return sorted(
            p.name.removesuffix(SUFFIX)
            for p in directory.iterdir()
            if p.name.endswith(SUFFIX) and not p.name.endswith(META_SUFFIX)
        )


def _write_atomic(path: Path, content: bytes) -> None:
    """Write via a temp file in the same directory and rename, so readers never see a partial."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
