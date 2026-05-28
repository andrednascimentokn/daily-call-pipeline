"""Thin wrapper over google-cloud-storage with JSON helpers and retry/backoff.

The bucket name is read from env `GCS_BUCKET`. The service account JSON is found
via `GOOGLE_APPLICATION_CREDENTIALS` (standard google-auth env var).

All operations are idempotent. Network errors are retried 3x with exponential
backoff before propagating.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, TypeVar

from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage

log = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_RETRIES = 3
_BACKOFF_BASE_SEC = 1.5


def _retry(fn: Callable[[], T], *, attempts: int = _DEFAULT_RETRIES, label: str = "op") -> T:
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except (GoogleAPIError, OSError) as exc:
            last_exc = exc
            sleep = _BACKOFF_BASE_SEC * (2 ** i)
            log.warning(f"GCS {label} failed (attempt {i+1}/{attempts}): {exc}. Retrying in {sleep:.1f}s")
            time.sleep(sleep)
    assert last_exc is not None
    raise last_exc


def _bucket() -> storage.Bucket:
    name = os.getenv("GCS_BUCKET", "").strip()
    if not name:
        raise RuntimeError("GCS_BUCKET env var is not set")
    client = storage.Client()
    return client.bucket(name)


def gcs_exists(path: str) -> bool:
    """Return True if a blob at `path` exists."""
    def _do() -> bool:
        return _bucket().blob(path).exists()
    return _retry(_do, label=f"exists {path}")


def gcs_read_json(path: str) -> Optional[dict]:
    """Read a JSON object from `path`, or None if it does not exist."""
    def _do() -> Optional[dict]:
        blob = _bucket().blob(path)
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        return json.loads(data.decode("utf-8"))
    return _retry(_do, label=f"read_json {path}")


def gcs_write_json(path: str, data: Any) -> None:
    """Upload `data` as pretty-printed JSON to `path`."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

    def _do() -> None:
        blob = _bucket().blob(path)
        blob.upload_from_file(io.BytesIO(payload), content_type="application/json", size=len(payload))
    _retry(_do, label=f"write_json {path}")


def gcs_upload_file(local: Path, remote: str, *, content_type: Optional[str] = None) -> None:
    """Upload a local file to `remote` in the bucket."""
    local = Path(local)
    if not local.exists():
        raise FileNotFoundError(local)

    def _do() -> None:
        blob = _bucket().blob(remote)
        blob.upload_from_filename(str(local), content_type=content_type)
    _retry(_do, label=f"upload_file {remote}")


def gcs_upload_folder(local_dir: Path, remote_prefix: str, *, skip_existing: bool = False) -> int:
    """Upload all files under `local_dir` recursively to `remote_prefix`.

    Returns the count of files uploaded. If `skip_existing` is True, blobs that
    already exist in the bucket are not re-uploaded (useful for idempotent migrations).
    """
    local_dir = Path(local_dir)
    if not local_dir.exists():
        return 0
    remote_prefix = remote_prefix.rstrip("/")
    uploaded = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        remote = f"{remote_prefix}/{rel}"
        if skip_existing and gcs_exists(remote):
            continue
        gcs_upload_file(path, remote)
        uploaded += 1
    return uploaded


def gcs_download_file(remote: str, local: Path) -> bool:
    """Download `remote` to `local`. Returns True on success, False if blob missing."""
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)

    def _do() -> bool:
        blob = _bucket().blob(remote)
        if not blob.exists():
            return False
        blob.download_to_filename(str(local))
        return True
    return _retry(_do, label=f"download {remote}")


def gcs_list_prefix(prefix: str) -> List[str]:
    """List all blob names under `prefix`."""
    def _do() -> List[str]:
        return [b.name for b in _bucket().list_blobs(prefix=prefix)]
    return _retry(_do, label=f"list {prefix}")
