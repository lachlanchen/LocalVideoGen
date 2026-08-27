"""Durable private registry for sequential H3 video-series projects.

The store owns only compact JSON state.  Source references remain in ComfyUI's
input area, render attempts remain in ComfyUI's output area, and derived
continuity/final artifacts remain below the private series artifact root.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final


SCHEMA_VERSION: Final = 1
MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
SERIES_STATUSES: Final = frozenset(
    {
        "ready",
        "queued",
        "waiting",
        "running",
        "pausing",
        "paused",
        "cancelling",
        "stitching",
        "completed",
        "failed",
        "cancelled",
    }
)
RUNNABLE_STATUSES: Final = frozenset(
    {"queued", "waiting", "running", "pausing", "cancelling", "stitching"}
)


class SeriesStoreError(RuntimeError):
    """Base error for a private series registry failure."""


class SeriesStoreValidationError(ValueError):
    """A caller attempted to persist malformed series state."""


class SeriesNotFoundError(SeriesStoreError):
    """The requested series is not registered."""


class SeriesAlreadyExistsError(SeriesStoreError):
    """The requested identifier is already registered."""


def canonical_series_id(value: str) -> str:
    if not isinstance(value, str):
        raise SeriesStoreValidationError("series id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SeriesStoreValidationError("series id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise SeriesStoreValidationError("series id must be a canonical UUID")
    return canonical


def _validated_status(value: str) -> str:
    if not isinstance(value, str) or value not in SERIES_STATUSES:
        raise SeriesStoreValidationError("invalid series status")
    return value


def _encoded_document(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise SeriesStoreValidationError("series document must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SeriesStoreValidationError("series document must contain finite JSON values") from exc
    if not isinstance(decoded, dict):
        raise SeriesStoreValidationError("series document must be a JSON object")
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise SeriesStoreValidationError("series document is too large")
    return decoded, encoded


def _database_error(exc: sqlite3.Error) -> SeriesStoreError:
    message = str(exc).lower()
    if any(marker in message for marker in ("malformed", "not a database", "disk image")):
        return SeriesStoreError("the series registry is corrupt")
    return SeriesStoreError("the series registry is unavailable")


class SeriesStore:
    """Small SQLite state store with atomic synchronous mutations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._last_timestamp_ms = 0
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise SeriesStoreError("the series registry directory must be private")
            if self.path.is_symlink():
                raise SeriesStoreError("the series registry cannot be a symbolic link")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
            os.chmod(self.path, 0o600)
        except SeriesStoreError:
            raise
        except OSError as exc:
            raise SeriesStoreError("could not prepare the private series registry") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    def _harden_files(self) -> None:
        try:
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                if candidate.exists():
                    if candidate.is_symlink():
                        raise SeriesStoreError("a series registry file cannot be a symbolic link")
                    os.chmod(candidate, 0o600)
        except SeriesStoreError:
            raise
        except OSError as exc:
            raise SeriesStoreError("could not protect the series registry") from exc

    def _initialize(self) -> None:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                if str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower() != "wal":
                    raise SeriesStoreError("the series registry could not enable WAL mode")
                connection.execute("PRAGMA synchronous = NORMAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise SeriesStoreError("the series registry schema is not supported")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS series (
                        series_id TEXT PRIMARY KEY NOT NULL,
                        status TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        created_ms INTEGER NOT NULL,
                        updated_ms INTEGER NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS series_status_updated ON series(status, updated_ms DESC)"
                )
                if version == 0:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
                expected = {
                    "series_id",
                    "status",
                    "document_json",
                    "revision",
                    "created_ms",
                    "updated_ms",
                }
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(series)")}
                if columns != expected:
                    raise SeriesStoreError("the series registry schema is invalid")
                check = connection.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise SeriesStoreError("the series registry failed its integrity check")
                row = connection.execute("SELECT COALESCE(MAX(updated_ms), 0) FROM series").fetchone()
                self._last_timestamp_ms = int(row[0]) if row else 0
                self._harden_files()
            except SeriesStoreError:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            except sqlite3.Error as exc:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def _timestamp(self, minimum: int = 0) -> int:
        value = max(int(time.time() * 1000), self._last_timestamp_ms + 1, minimum)
        self._last_timestamp_ms = value
        return value

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        try:
            document = json.loads(row["document_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SeriesStoreError("a series record contains invalid JSON") from exc
        decoded, _ = _encoded_document(document)
        return {
            "id": canonical_series_id(str(row["series_id"])),
            "status": _validated_status(str(row["status"])),
            "document": decoded,
            "revision": int(row["revision"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
        }

    def create(
        self,
        series_id: str,
        document: Mapping[str, Any],
        *,
        status: str = "ready",
    ) -> dict[str, Any]:
        canonical = canonical_series_id(series_id)
        state = _validated_status(status)
        _, encoded = _encoded_document(document)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                timestamp = self._timestamp()
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "INSERT INTO series VALUES (?, ?, ?, 1, ?, ?)",
                        (canonical, state, encoded, timestamp, timestamp),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise SeriesAlreadyExistsError("series id is already registered") from exc
                row = connection.execute("SELECT * FROM series WHERE series_id = ?", (canonical,)).fetchone()
                connection.commit()
                if row is None:
                    raise SeriesStoreError("the new series could not be read back")
                return self._decode(row)
            except SeriesStoreError:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            except sqlite3.Error as exc:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def get(self, series_id: str) -> dict[str, Any] | None:
        canonical = canonical_series_id(series_id)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                row = connection.execute(
                    "SELECT * FROM series WHERE series_id = ?", (canonical,)
                ).fetchone()
                return self._decode(row) if row is not None else None
            except SeriesStoreError:
                raise
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def list(self, *, limit: int = 40, runnable: bool = False) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SeriesStoreValidationError("limit must be between 1 and 100")
        parameters: list[Any] = []
        where = ""
        if runnable:
            placeholders = ",".join("?" for _ in RUNNABLE_STATUSES)
            where = f"WHERE status IN ({placeholders})"
            parameters.extend(sorted(RUNNABLE_STATUSES))
        parameters.append(limit)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                direction = "ASC" if runnable else "DESC"
                rows = connection.execute(
                    f"SELECT * FROM series {where} ORDER BY updated_ms {direction}, created_ms {direction} LIMIT ?",
                    parameters,
                ).fetchall()
                return [self._decode(row) for row in rows]
            except SeriesStoreError:
                raise
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def mutate(
        self,
        series_id: str,
        callback: Callable[[dict[str, Any], str], tuple[Mapping[str, Any], str] | None],
    ) -> dict[str, Any]:
        """Atomically mutate a document and status.

        Returning ``None`` leaves the row untouched.  The callback receives a
        deep JSON copy, so retaining or modifying it later cannot change state.
        """

        canonical = canonical_series_id(series_id)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM series WHERE series_id = ?", (canonical,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise SeriesNotFoundError("series not found")
                current = self._decode(row)
                result = callback(copy.deepcopy(current["document"]), str(current["status"]))
                if result is None:
                    connection.rollback()
                    return current
                document, status = result
                state = _validated_status(status)
                _, encoded = _encoded_document(document)
                revision = int(current["revision"]) + 1
                updated = self._timestamp(int(current["updated_ms"]) + 1)
                connection.execute(
                    "UPDATE series SET status = ?, document_json = ?, revision = ?, updated_ms = ? WHERE series_id = ?",
                    (state, encoded, revision, updated, canonical),
                )
                row = connection.execute(
                    "SELECT * FROM series WHERE series_id = ?", (canonical,)
                ).fetchone()
                connection.commit()
                if row is None:
                    raise SeriesStoreError("the updated series could not be read back")
                return self._decode(row)
            except SeriesStoreError:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            except sqlite3.Error as exc:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()
