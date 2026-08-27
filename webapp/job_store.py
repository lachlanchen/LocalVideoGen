"""Private, durable ownership and history registry for H3 render jobs.

The registry deliberately stores only small render metadata (including the
prompt) and normalized ComfyUI output locators. Uploaded and generated media
remain in their existing ComfyUI locations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final


SCHEMA_VERSION: Final = 1
DEFAULT_MAX_TERMINAL_JOBS: Final = 500
MAX_METADATA_BYTES: Final = 64 * 1024
MAX_OUTPUTS_BYTES: Final = 256 * 1024
MAX_OUTPUTS: Final = 128
MAX_ERROR_LENGTH: Final = 8_192

ACTIVE_STATUSES: Final = frozenset(
    {"submitting", "queued", "pending", "in_progress", "running", "cancelling"}
)
TERMINAL_STATUSES: Final = frozenset(
    {"completed", "success", "failed", "error", "cancelled"}
)
ALLOWED_STATUSES: Final = ACTIVE_STATUSES | TERMINAL_STATUSES
_MEDIA_TYPE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_UNSET: Final = object()


class JobStoreError(RuntimeError):
    """Base class for registry failures safe to surface as a generic 5xx."""


class JobStoreCorruptionError(JobStoreError):
    """The SQLite database or one of its persisted JSON records is invalid."""


class JobStoreValidationError(ValueError):
    """An attempted registry write contains invalid or unsafe data."""


class JobAlreadyExistsError(JobStoreError):
    """A job identifier is already owned by this webapp registry."""


class JobNotFoundError(JobStoreError):
    """A requested job is not owned by this webapp registry."""


def canonical_job_id(value: str) -> str:
    """Return a strict lowercase, hyphenated UUID or reject the value."""

    if not isinstance(value, str):
        raise JobStoreValidationError("job id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise JobStoreValidationError("job id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise JobStoreValidationError("job id must be a canonical UUID")
    return canonical


def _validated_status(value: str) -> str:
    if not isinstance(value, str) or value not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise JobStoreValidationError(f"invalid job status; expected one of: {allowed}")
    return value


def _json_object(value: Mapping[str, Any], *, label: str) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise JobStoreValidationError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise JobStoreValidationError(f"{label} must contain only finite JSON values") from exc
    if not isinstance(decoded, dict):  # Defensive: Mapping conversion should guarantee this.
        raise JobStoreValidationError(f"{label} must be a JSON object")
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise JobStoreValidationError(f"{label} is too large")
    return decoded, encoded


def _safe_text(value: Any, *, label: str, maximum: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise JobStoreValidationError(f"output {label} is invalid")
    if len(value) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise JobStoreValidationError(f"output {label} is invalid")
    return value


def _safe_output(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise JobStoreValidationError("each output must be a JSON object")

    filename = _safe_text(raw.get("filename"), label="filename", maximum=255, allow_empty=False)
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise JobStoreValidationError("output filename is unsafe")
    if PurePosixPath(filename).name != filename:
        raise JobStoreValidationError("output filename is unsafe")

    subfolder = _safe_text(raw.get("subfolder", ""), label="subfolder", maximum=512)
    if "\\" in subfolder:
        raise JobStoreValidationError("output subfolder is unsafe")
    if subfolder:
        folder = PurePosixPath(subfolder)
        if folder.is_absolute() or any(part in {"", ".", ".."} for part in subfolder.split("/")):
            raise JobStoreValidationError("output subfolder is unsafe")

    if raw.get("type") != "output":
        raise JobStoreValidationError("output type must be 'output'")
    media_type = _safe_text(raw.get("media_type"), label="media type", maximum=32, allow_empty=False)
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise JobStoreValidationError("output media type is invalid")
    node_id = _safe_text(raw.get("node_id", ""), label="node id", maximum=128)

    output_id = raw.get("id", index)
    if isinstance(output_id, bool) or not isinstance(output_id, int) or not 0 <= output_id <= 100_000:
        raise JobStoreValidationError("output id is invalid")
    return {
        "id": output_id,
        "filename": filename,
        "subfolder": subfolder,
        "type": "output",
        "media_type": media_type,
        "node_id": node_id,
    }


def _normalized_outputs(value: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise JobStoreValidationError("outputs must be a JSON array")
    if len(value) > MAX_OUTPUTS:
        raise JobStoreValidationError(f"outputs accepts at most {MAX_OUTPUTS} items")
    outputs = [_safe_output(item, index) for index, item in enumerate(value)]
    ids = [item["id"] for item in outputs]
    if len(ids) != len(set(ids)):
        raise JobStoreValidationError("output ids must be unique")
    encoded = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_OUTPUTS_BYTES:
        raise JobStoreValidationError("outputs are too large")
    return outputs, encoded


def _database_error(exc: sqlite3.Error) -> JobStoreError:
    message = str(exc).lower()
    if any(marker in message for marker in ("malformed", "not a database", "disk image")):
        return JobStoreCorruptionError("the job registry is corrupt")
    return JobStoreError("the job registry is unavailable")


class JobStore:
    """A compact SQLite registry using one short connection per operation.

    The containing directory must be private (0700 or stricter).  A separate
    SQLite connection plus a process lock per operation makes this safe to call
    from aiohttp worker threads without retaining thread-bound connections.
    """

    def __init__(self, path: str | Path, *, max_terminal_jobs: int = DEFAULT_MAX_TERMINAL_JOBS) -> None:
        if isinstance(max_terminal_jobs, bool) or not isinstance(max_terminal_jobs, int):
            raise JobStoreValidationError("max_terminal_jobs must be an integer")
        if not 1 <= max_terminal_jobs <= 100_000:
            raise JobStoreValidationError("max_terminal_jobs must be between 1 and 100000")
        self.path = Path(path)
        self.max_terminal_jobs = max_terminal_jobs
        self._lock = threading.RLock()
        self._last_timestamp_ms = 0
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent_existed = self.path.parent.exists()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not parent_existed:
                os.chmod(self.path.parent, 0o700)
            parent_mode = stat.S_IMODE(self.path.parent.stat().st_mode)
            if parent_mode & 0o077:
                raise JobStoreError("the job registry directory must be private")
            if self.path.is_symlink():
                raise JobStoreError("the job registry cannot be a symbolic link")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
            os.chmod(self.path, 0o600)
        except JobStoreError:
            raise
        except OSError as exc:
            raise JobStoreError("could not prepare the private job registry") from exc

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
                        raise JobStoreError("a job registry file cannot be a symbolic link")
                    os.chmod(candidate, 0o600)
        except JobStoreError:
            raise
        except OSError as exc:
            raise JobStoreError("could not protect the job registry") from exc

    def _initialize(self) -> None:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
                if journal_mode != "wal":
                    raise JobStoreError("the job registry could not enable WAL mode")
                connection.execute("PRAGMA synchronous = NORMAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise JobStoreError("the job registry schema is not supported")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY NOT NULL,
                        status TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        outputs_json TEXT NOT NULL,
                        error TEXT,
                        created_ms INTEGER NOT NULL,
                        updated_ms INTEGER NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_status_updated ON jobs(status, updated_ms DESC)"
                )
                if version == 0:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()

                expected_columns = {
                    "job_id",
                    "status",
                    "metadata_json",
                    "outputs_json",
                    "error",
                    "created_ms",
                    "updated_ms",
                }
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")}
                if columns != expected_columns:
                    raise JobStoreCorruptionError("the job registry schema is invalid")
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if not quick_check or str(quick_check[0]).lower() != "ok":
                    raise JobStoreCorruptionError("the job registry failed its integrity check")
                row = connection.execute("SELECT COALESCE(MAX(updated_ms), 0) FROM jobs").fetchone()
                self._last_timestamp_ms = int(row[0]) if row else 0
                self._harden_files()
            except JobStoreError:
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

    def _next_timestamp(self, minimum: int = 0) -> int:
        value = max(int(time.time() * 1000), self._last_timestamp_ms + 1, minimum)
        self._last_timestamp_ms = value
        return value

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"])
            outputs = json.loads(row["outputs_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise JobStoreCorruptionError("a job registry record contains invalid JSON") from exc
        if not isinstance(metadata, dict) or not isinstance(outputs, list):
            raise JobStoreCorruptionError("a job registry record has an invalid shape")
        try:
            canonical_metadata, _ = _json_object(metadata, label="metadata")
            normalized, _ = _normalized_outputs(outputs)
            status = _validated_status(str(row["status"]))
            job_id = canonical_job_id(str(row["job_id"]))
        except JobStoreValidationError as exc:
            raise JobStoreCorruptionError("a job registry record contains unsafe data") from exc
        if canonical_metadata != metadata or normalized != outputs:
            raise JobStoreCorruptionError("a job registry record contains non-canonical JSON data")
        error = row["error"]
        if error is not None and (not isinstance(error, str) or len(error) > MAX_ERROR_LENGTH):
            raise JobStoreCorruptionError("a job registry record contains an invalid error")
        created_ms = row["created_ms"]
        updated_ms = row["updated_ms"]
        if (
            isinstance(created_ms, bool)
            or not isinstance(created_ms, int)
            or isinstance(updated_ms, bool)
            or not isinstance(updated_ms, int)
            or created_ms < 0
            or updated_ms < created_ms
        ):
            raise JobStoreCorruptionError("a job registry record contains invalid timestamps")
        return {
            "id": job_id,
            "status": status,
            "metadata": metadata,
            "outputs": normalized,
            "error": error,
            "created_ms": created_ms,
            "updated_ms": updated_ms,
        }

    def _prune_terminal(self, connection: sqlite3.Connection) -> None:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        stale = connection.execute(
            f"""
            SELECT job_id FROM jobs
            WHERE status IN ({placeholders})
            ORDER BY updated_ms DESC, created_ms DESC, job_id DESC
            LIMIT -1 OFFSET ?
            """,
            (*sorted(TERMINAL_STATUSES), self.max_terminal_jobs),
        ).fetchall()
        if stale:
            connection.executemany("DELETE FROM jobs WHERE job_id = ?", ((row[0],) for row in stale))

    def register(
        self,
        job_id: str,
        metadata: Mapping[str, Any],
        *,
        status: str = "submitting",
    ) -> dict[str, Any]:
        """Claim a new UUID and persist its initial render metadata."""

        canonical = canonical_job_id(job_id)
        state = _validated_status(status)
        decoded_metadata, encoded_metadata = _json_object(metadata, label="metadata")
        del decoded_metadata  # The row decoder provides the independent return value.
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                timestamp = self._next_timestamp()
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        INSERT INTO jobs
                            (job_id, status, metadata_json, outputs_json, error, created_ms, updated_ms)
                        VALUES (?, ?, ?, '[]', NULL, ?, ?)
                        """,
                        (canonical, state, encoded_metadata, timestamp, timestamp),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise JobAlreadyExistsError("job id is already registered") from exc
                self._prune_terminal(connection)
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (canonical,)).fetchone()
                connection.commit()
                self._harden_files()
                if row is None:
                    raise JobStoreError("the new job could not be read back")
                return self._decode_row(row)
            except JobStoreError:
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

    def update(
        self,
        job_id: str,
        status: str | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        outputs: Sequence[Mapping[str, Any]] | None = None,
        error: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Atomically update selected fields and return the resulting record."""

        canonical = canonical_job_id(job_id)
        state = _validated_status(status) if status is not None else None
        encoded_metadata = _json_object(metadata, label="metadata")[1] if metadata is not None else None
        encoded_outputs = _normalized_outputs(outputs)[1] if outputs is not None else None
        if error is not _UNSET:
            if error is not None and (not isinstance(error, str) or len(error) > MAX_ERROR_LENGTH):
                raise JobStoreValidationError(f"error must be null or at most {MAX_ERROR_LENGTH} characters")

        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT updated_ms FROM jobs WHERE job_id = ?", (canonical,)
                ).fetchone()
                if existing is None:
                    connection.rollback()
                    raise JobNotFoundError("job is not registered")

                assignments = ["updated_ms = ?"]
                parameters: list[Any] = [self._next_timestamp(int(existing[0]) + 1)]
                if state is not None:
                    assignments.append("status = ?")
                    parameters.append(state)
                if encoded_metadata is not None:
                    assignments.append("metadata_json = ?")
                    parameters.append(encoded_metadata)
                if encoded_outputs is not None:
                    assignments.append("outputs_json = ?")
                    parameters.append(encoded_outputs)
                if error is not _UNSET:
                    assignments.append("error = ?")
                    parameters.append(error)
                parameters.append(canonical)
                connection.execute(
                    f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
                    parameters,
                )
                self._prune_terminal(connection)
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (canonical,)).fetchone()
                connection.commit()
                self._harden_files()
                if row is None:
                    raise JobStoreError("the updated job could not be read back")
                return self._decode_row(row)
            except JobStoreError:
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

    def get(self, job_id: str) -> dict[str, Any] | None:
        canonical = canonical_job_id(job_id)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (canonical,)).fetchone()
                return self._decode_row(row) if row is not None else None
            except JobStoreError:
                raise
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def status(self, job_id: str) -> str | None:
        record = self.get(job_id)
        return str(record["status"]) if record is not None else None

    def owns(self, job_id: str) -> bool:
        """Fail closed for malformed or unknown IDs, convenient for HTTP guards."""

        try:
            canonical = canonical_job_id(job_id)
        except JobStoreValidationError:
            return False
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                return (
                    connection.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ? LIMIT 1", (canonical,)
                    ).fetchone()
                    is not None
                )
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def list(self, *, scope: str = "all", limit: int = 40) -> list[dict[str, Any]]:
        if scope not in {"all", "active", "history"}:
            raise JobStoreValidationError("scope must be all, active, or history")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise JobStoreValidationError("limit must be between 1 and 1000")
        parameters: list[Any] = []
        where = ""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        terminal = sorted(TERMINAL_STATUSES)
        if scope == "active":
            where = f"WHERE status NOT IN ({placeholders})"
            parameters.extend(terminal)
        elif scope == "history":
            where = f"WHERE status IN ({placeholders})"
            parameters.extend(terminal)
        parameters.append(limit)
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                rows = connection.execute(
                    f"SELECT * FROM jobs {where} ORDER BY updated_ms DESC, created_ms DESC, job_id DESC LIMIT ?",
                    parameters,
                ).fetchall()
                return [self._decode_row(row) for row in rows]
            except JobStoreError:
                raise
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._harden_files()

    def active(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.list(scope="active", limit=limit)
