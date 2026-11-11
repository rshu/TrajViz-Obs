"""Manage revocable bearer credentials for a telemetry collector."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from bridge.bootstrap import atomic_json

REGISTRY_ENV = "TRAJVIZ_OBS_INGEST_CREDENTIALS"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_BEARER = re.compile(r"Bearer +([A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)
_MAX_CREDENTIALS = 8192
_MAX_REGISTRY_BYTES = 2 * 1024 * 1024


def registry_path(path: str | Path | None, *, required: bool) -> Path | None:
    raw = path if path is not None else os.environ.get(REGISTRY_ENV)
    if raw is None and not required:
        return None
    if raw is None or not (candidate := Path(raw).expanduser()).is_absolute():
        raise ValueError(f"Set {REGISTRY_ENV} to the absolute path of the ingestion credential registry.")
    # Preserve the configured name so atomic replacement and mounted symlinks can rotate the registry.
    return candidate


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate field in the ingestion credential registry.")
    return value


@contextmanager
def _open_registry(path: Path) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o022):
            raise ValueError("The ingestion credential registry must be a regular file writable only by its owner.")
        if info.st_size > _MAX_REGISTRY_BYTES:
            raise ValueError("The ingestion credential registry is too large.")
        yield handle, info


def _decode_registry(handle: BinaryIO) -> dict[str, str]:
    try:
        encoded = handle.read(_MAX_REGISTRY_BYTES + 1)
        if len(encoded) > _MAX_REGISTRY_BYTES:
            raise ValueError("The ingestion credential registry is too large.")
        data = json.loads(encoded, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        raise ValueError("Invalid ingestion credential registry.") from None
    if (not isinstance(data, dict) or set(data) != {"version", "credentials"}
            or type(data["version"]) is not int or data["version"] != 1):
        raise ValueError("Invalid ingestion credential registry.")
    values = data["credentials"]
    if (not isinstance(values, dict) or len(values) > _MAX_CREDENTIALS
            or any(not _NAME.fullmatch(name) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                   for name, digest in values.items())
            or len(set(values.values())) != len(values)):
        raise ValueError("Invalid ingestion credential registry.")
    return values


def _read_registry(path: Path, *, missing_ok: bool = False) -> dict[str, str]:
    try:
        with _open_registry(path) as (handle, _):
            return _decode_registry(handle)
    except FileNotFoundError:
        if not missing_ok or path.is_symlink():
            raise
        return {}


class CredentialRegistry:
    """Reload changed credentials in every worker and fail closed if the file is unavailable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self._revision: tuple[int, ...] | None = None
        self._digests: tuple[str, ...] = ()
        self._current()

    def _current(self) -> tuple[str, ...]:
        with self._lock, _open_registry(self.path) as (handle, info):
            revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if revision != self._revision:
                self._digests = tuple(_decode_registry(handle).values())
                self._revision = revision
            return self._digests

    def authenticate(self, authorization: str) -> str | None:
        """Return an opaque key for rate limiting without persisting the bearer credential."""
        match = _BEARER.fullmatch(authorization) if len(authorization) <= 64 else None
        if match is None:
            return None
        digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()
        found = False
        for registered in self._current():
            found |= hmac.compare_digest(digest, registered)
        if not found:
            return None
        return hashlib.sha256(b"trajviz-obs ingress rate\0" + bytes.fromhex(digest)).hexdigest()


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink():
        raise ValueError("The ingestion credential lock cannot be a symbolic link.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(lock_path, flags, 0o600), "r+b") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o022):
            raise ValueError("The ingestion credential lock must be a regular file writable only by its owner.")
        if os.name == "nt":
            import msvcrt

            if not info.st_size:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        # The handle releases the lock at close, including on process exit.
        if path.is_symlink():
            raise ValueError("Update the credential registry at its target path, away from symbolic links.")
        yield


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError("Credential names must use 1–64 letters, digits, periods, hyphens, or underscores and start with a letter or digit.")


def issue_credential(path: Path, name: str) -> str:
    """Issue a 256-bit random token. Its plaintext is returned only to the caller."""
    _check_name(name)
    with _registry_lock(path):
        values = _read_registry(path, missing_ok=True)
        if name in values:
            raise ValueError("A credential with that name already exists.")
        if len(values) >= _MAX_CREDENTIALS:
            raise ValueError("The ingestion credential registry is full.")
        token = secrets.token_urlsafe(32)
        values[name] = hashlib.sha256(token.encode("ascii")).hexdigest()
        atomic_json(path, {"version": 1, "credentials": values})
    return token


def revoke_credential(path: Path, name: str) -> bool:
    _check_name(name)
    with _registry_lock(path):
        values = _read_registry(path, missing_ok=True)
        if name not in values:
            return False
        del values[name]
        atomic_json(path, {"version": 1, "credentials": values})
    return True


def list_credentials(path: Path) -> tuple[str, ...]:
    """List operator-assigned names without returning digests or token material."""
    return tuple(sorted(_read_registry(path, missing_ok=True)))
