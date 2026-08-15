"""Headless batch orchestration shared by the GUI and MCP server."""

from __future__ import annotations

import hashlib
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .canvas_client import (
    CanvasAssignment,
    CanvasClient,
    CanvasCourse,
    CanvasError,
    UncertainSubmissionError,
    UploadedFile,
)
from .result_log import write_results_log
from .submission_guard import ApprovalSnapshot, create_approval


DEFAULT_APPROVAL_TTL_SECONDS = 10 * 60


class BatchServiceError(ValueError):
    """A safe, caller-facing batch workflow error."""


class BatchNotFoundError(BatchServiceError):
    """The requested in-memory approval is unavailable."""


class BatchExpiredError(BatchServiceError):
    """The requested approval has expired."""


class BatchChangedError(BatchServiceError):
    """A file or assignment changed after approval."""


class SubmissionDisabledError(BatchServiceError):
    """The server was not explicitly started in write-enabled mode."""


class CanvasOperations(Protocol):
    """Canvas operations used by the shared workflow and offline fakes."""

    def get_active_courses(self) -> list[CanvasCourse]: ...

    def get_assignments(self, course_id: str) -> list[CanvasAssignment]: ...

    def ensure_assignment_is_unsubmitted(
        self, course_id: str, assignment_id: str
    ) -> None: ...

    def upload_file(
        self, course_id: str, assignment_id: str, path: Path
    ) -> UploadedFile: ...

    def create_submission(
        self,
        course_id: str,
        assignment_id: str,
        uploaded_files: Iterable[UploadedFile],
        *,
        approval: ApprovalSnapshot | None,
    ) -> Mapping[str, object]: ...

    def close(self) -> None: ...


@dataclass
class BatchRunResult:
    """One attempt's uploads and final-submission outcome."""

    uploaded: dict[Path, UploadedFile]
    upload_errors: dict[Path, str]
    submitted: bool = False
    submission_error: str | None = None
    submission_uncertain: bool = False
    retry_safe: bool = False


ProgressCallback = Callable[[int, str], None]
ErrorSanitizer = Callable[[BaseException], str]


def _default_safe_error(error: BaseException) -> str:
    if isinstance(error, CanvasError):
        return str(error)
    return "Unexpected application error."


def run_approved_batch(
    client: CanvasOperations,
    course: CanvasCourse,
    assignment: CanvasAssignment,
    approved_paths: Sequence[Path],
    approval: ApprovalSnapshot,
    *,
    uploaded: Mapping[Path, UploadedFile] | None = None,
    retry_paths: Iterable[Path] | None = None,
    progress: ProgressCallback | None = None,
    safe_error: ErrorSanitizer = _default_safe_error,
) -> BatchRunResult:
    """Upload an approved batch and create one submission after every upload succeeds."""
    receipts = dict(uploaded or {})
    paths_to_upload = list(retry_paths if retry_paths is not None else approved_paths)
    errors: dict[Path, str] = {}

    def report(value: int, message: str) -> None:
        if progress is not None:
            progress(value, message)

    try:
        client.ensure_assignment_is_unsubmitted(course.id, assignment.id)
    except CanvasError as error:
        message = safe_error(error)
        return BatchRunResult(receipts, {path: message for path in approved_paths})
    except Exception as error:
        message = safe_error(error)
        return BatchRunResult(receipts, {path: message for path in approved_paths})

    report(1, "Assignment is unsubmitted. Starting approved uploads...")
    for index, path in enumerate(paths_to_upload, start=1):
        report(index, f"Uploading {path.name}...")
        try:
            receipts[path] = client.upload_file(course.id, assignment.id, path)
        except CanvasError as error:
            errors[path] = safe_error(error)
        except Exception as error:
            errors[path] = safe_error(error)
        report(index + 1, f"Finished upload attempt for {path.name}.")

    missing = [path for path in approved_paths if path not in receipts]
    if errors or missing:
        for path in missing:
            errors.setdefault(path, "The file has not uploaded successfully.")
        return BatchRunResult(receipts, errors, retry_safe=True)

    report(len(paths_to_upload) + 1, "All files uploaded. Rechecking and submitting...")
    try:
        ordered_uploads = [receipts[path] for path in approved_paths]
        client.create_submission(
            course.id,
            assignment.id,
            ordered_uploads,
            approval=approval,
        )
    except UncertainSubmissionError as error:
        return BatchRunResult(
            receipts,
            {},
            submission_error=safe_error(error),
            submission_uncertain=True,
        )
    except CanvasError as error:
        return BatchRunResult(receipts, {}, submission_error=safe_error(error))
    except Exception as error:
        return BatchRunResult(receipts, {}, submission_error=safe_error(error))
    return BatchRunResult(receipts, {}, submitted=True)


def is_sensitive_local_file(path: Path) -> bool:
    """Return whether a file name is an obvious credential or private-key file."""
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    if "credential" in name or name in {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }:
        return True
    if suffix in {".key", ".pem", ".p12", ".pfx"}:
        return True
    return name.endswith(("_rsa", "_dsa", "_ecdsa", "_ed25519"))


def list_local_files(folder_path: str) -> list[dict[str, object]]:
    """List safe regular files directly within one absolute local folder."""
    folder = Path(folder_path)
    if not folder.is_absolute():
        raise BatchServiceError("folder_path must be an absolute path.")
    try:
        folder = folder.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise BatchServiceError("The folder does not exist or is inaccessible.") from error
    if not folder.is_dir():
        raise BatchServiceError("The supplied path is not a folder.")

    files: list[dict[str, object]] = []
    try:
        entries = list(folder.iterdir())
    except OSError as error:
        raise BatchServiceError("The folder is inaccessible.") from error
    for entry in sorted(entries, key=lambda item: item.name.casefold()):
        if is_sensitive_local_file(entry) or entry.is_symlink():
            continue
        try:
            details = entry.stat()
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode):
            files.append(
                {
                    "file_name": entry.name,
                    "full_path": str(entry.resolve(strict=False)),
                    "size_bytes": details.st_size,
                }
            )
    return files


@dataclass(frozen=True)
class FileApproval:
    """Immutable identity and content snapshot for one approved file."""

    path: Path
    size: int
    modified_ns: int
    content_sha256: str = field(repr=False)


@dataclass(frozen=True)
class PreparedApproval:
    """The complete immutable target and file approval snapshot."""

    batch_id: str
    course: CanvasCourse
    assignment: CanvasAssignment
    files: tuple[FileApproval, ...]
    approval: ApprovalSnapshot
    created_at_utc: str
    expires_at_monotonic: float = field(repr=False)


@dataclass
class _PreparedState:
    approval: PreparedApproval
    uploaded: dict[Path, UploadedFile] = field(default_factory=dict)
    failed_paths: set[Path] = field(default_factory=set)
    status: str = "prepared"


class PreparedBatchManager:
    """Own expiring approval state and serialize all submission operations."""

    def __init__(
        self,
        client: CanvasOperations,
        project_root: Path,
        *,
        submit_enabled: bool = False,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        secrets: Iterable[str] = (),
    ) -> None:
        if approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive.")
        self._client = client
        self._project_root = project_root
        self._submit_enabled = submit_enabled
        self._ttl = approval_ttl_seconds
        self._clock = clock
        self._secrets = tuple(secret for secret in secrets if secret)
        self._batches: dict[str, _PreparedState] = {}
        self._consumed: set[str] = set()
        self._expired: set[str] = set()
        self._invalid: set[str] = set()
        self._lock = threading.RLock()

    @property
    def approval_ttl_seconds(self) -> int:
        return self._ttl

    def close(self) -> None:
        with self._lock:
            self._batches.clear()
            self._consumed.clear()
            self._expired.clear()
            self._invalid.clear()
            self._client.close()

    def list_active_courses(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"course_id": course.id, "course_name": course.name}
                for course in self._client.get_active_courses()
            ]

    def list_assignments(self, course_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [
                self._assignment_record(assignment)
                for assignment in self._client.get_assignments(course_id)
            ]

    def prepare(
        self, course_id: str, assignment_id: str, file_paths: Sequence[str]
    ) -> dict[str, object]:
        with self._lock:
            self._prune_expired()
            course, assignment = self._resolve_target(course_id, assignment_id)
            files = self._snapshot_files(file_paths, assignment)
            self._client.ensure_assignment_is_unsubmitted(course.id, assignment.id)
            paths = tuple(item.path for item in files)
            approval = create_approval(
                confirmed=True,
                course_id=course.id,
                assignment_id=assignment.id,
                file_paths=paths,
            )
            batch_id = uuid.uuid4().hex
            prepared = PreparedApproval(
                batch_id=batch_id,
                course=course,
                assignment=assignment,
                files=files,
                approval=approval,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                expires_at_monotonic=self._clock() + self._ttl,
            )
            self._batches[batch_id] = _PreparedState(prepared)
            return self._review_record(prepared)

    def submit(self, batch_id: str, confirmation: str) -> dict[str, object]:
        with self._lock:
            state = self._require_actionable(batch_id, confirmation)
            if state.status != "prepared":
                raise BatchServiceError(
                    "This batch has already been attempted; use "
                    "canvas_retry_failed_uploads only when uploads failed."
                )
            return self._execute(state)

    def retry_failed(self, batch_id: str, confirmation: str) -> dict[str, object]:
        with self._lock:
            state = self._require_actionable(batch_id, confirmation)
            if state.status == "submission_uncertain":
                raise BatchServiceError(
                    "The final submission status is uncertain. Do not retry; verify in Canvas."
                )
            if state.status != "upload_failed" or not state.failed_paths:
                raise BatchServiceError(
                    "This batch has no failed uploads that are safe to retry."
                )
            retry_paths = tuple(
                item.path
                for item in state.approval.files
                if item.path in state.failed_paths
            )
            return self._execute(state, retry_paths=retry_paths)

    def _execute(
        self, state: _PreparedState, *, retry_paths: Sequence[Path] | None = None
    ) -> dict[str, object]:
        prepared = state.approval
        course, assignment = self._resolve_target(
            prepared.course.id, prepared.assignment.id
        )
        if assignment != prepared.assignment or course != prepared.course:
            self._invalidate(prepared.batch_id)
            raise BatchChangedError(
                "The course or assignment changed after preparation. Prepare a new batch."
            )
        current_files = self._snapshot_files(
            [str(item.path) for item in prepared.files], assignment
        )
        if current_files != prepared.files:
            self._invalidate(prepared.batch_id)
            raise BatchChangedError(
                "One or more approved files changed after preparation. Prepare a new batch."
            )
        self._client.ensure_assignment_is_unsubmitted(course.id, assignment.id)
        paths = tuple(item.path for item in prepared.files)
        result = run_approved_batch(
            self._client,
            course,
            assignment,
            paths,
            prepared.approval,
            uploaded=state.uploaded,
            retry_paths=retry_paths,
            safe_error=self._safe_error,
        )
        state.uploaded = result.uploaded
        state.failed_paths = set(result.upload_errors)

        if result.submitted:
            state.status = "submitted"
            response = self._outcome_record(state, result, "submitted")
            self._batches.pop(prepared.batch_id, None)
            self._consumed.add(prepared.batch_id)
            return response
        if result.submission_uncertain:
            state.status = "submission_uncertain"
            return self._outcome_record(state, result, "submission_uncertain")
        if result.submission_error:
            state.status = "submission_failed"
            return self._outcome_record(state, result, "submission_failed")
        state.status = "upload_failed"
        return self._outcome_record(state, result, "upload_failed")

    def _require_actionable(
        self, batch_id: str, confirmation: str
    ) -> _PreparedState:
        if not self._submit_enabled:
            raise SubmissionDisabledError(
                "Submission tools are disabled. Restart with --enable-submit to allow writes."
            )
        self._prune_expired()
        if batch_id in self._consumed:
            raise BatchServiceError("This approval was already used successfully.")
        if batch_id in self._expired:
            raise BatchExpiredError("This approval expired. Prepare a new batch.")
        if batch_id in self._invalid:
            raise BatchChangedError("This approval is no longer valid. Prepare a new batch.")
        state = self._batches.get(batch_id)
        if state is None:
            raise BatchNotFoundError("No prepared batch exists for that batch ID.")
        if confirmation != f"SUBMIT {batch_id}":
            raise BatchServiceError(
                f"Exact confirmation required: SUBMIT {batch_id}"
            )
        return state

    def _resolve_target(
        self, course_id: str, assignment_id: str
    ) -> tuple[CanvasCourse, CanvasAssignment]:
        requested_course = str(course_id)
        requested_assignment = str(assignment_id)
        course = next(
            (item for item in self._client.get_active_courses() if item.id == requested_course),
            None,
        )
        if course is None:
            raise BatchServiceError("The requested active course was not found.")
        assignment = next(
            (
                item
                for item in self._client.get_assignments(course.id)
                if item.id == requested_assignment
            ),
            None,
        )
        if assignment is None:
            raise BatchServiceError("The requested assignment was not found.")
        if not assignment.accepts_file_uploads:
            raise BatchServiceError("The assignment does not accept online file uploads.")
        return course, assignment

    def _snapshot_files(
        self, file_paths: Sequence[str], assignment: CanvasAssignment
    ) -> tuple[FileApproval, ...]:
        if not file_paths:
            raise BatchServiceError("At least one absolute file path is required.")
        snapshots: list[FileApproval] = []
        seen: set[Path] = set()
        for raw_path in file_paths:
            path = Path(raw_path)
            if not path.is_absolute():
                raise BatchServiceError(f"File paths must be absolute: {raw_path}")
            if path.is_symlink():
                raise BatchServiceError(f"Symbolic links are not allowed: {path.name}")
            try:
                path = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise BatchServiceError(
                    f"The local file is missing or inaccessible: {path.name}"
                ) from error
            if path in seen:
                raise BatchServiceError(f"Duplicate file path: {path}")
            seen.add(path)
            if is_sensitive_local_file(path):
                raise BatchServiceError(
                    f"Credential and private-key files cannot be approved: {path.name}"
                )
            snapshots.append(self._fingerprint(path, assignment))
        return tuple(snapshots)

    @staticmethod
    def _fingerprint(path: Path, assignment: CanvasAssignment) -> FileApproval:
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise BatchServiceError(f"The path is not a regular file: {path}")
            extension = path.suffix.lstrip(".").casefold()
            if assignment.allowed_extensions and extension not in assignment.allowed_extensions:
                allowed = ", ".join(assignment.allowed_extensions)
                raise BatchServiceError(
                    f"Canvas allows only {allowed}; {path.name} is not allowed."
                )
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
        except BatchServiceError:
            raise
        except OSError as error:
            raise BatchServiceError(
                f"The local file is missing or inaccessible: {path.name}"
            ) from error
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BatchChangedError(
                f"The local file changed while it was being reviewed: {path.name}"
            )
        return FileApproval(path, after.st_size, after.st_mtime_ns, digest.hexdigest())

    def _prune_expired(self) -> None:
        now = self._clock()
        for batch_id, state in list(self._batches.items()):
            if now >= state.approval.expires_at_monotonic:
                self._batches.pop(batch_id, None)
                self._expired.add(batch_id)

    def _invalidate(self, batch_id: str) -> None:
        self._batches.pop(batch_id, None)
        self._invalid.add(batch_id)

    def _safe_error(self, error: BaseException) -> str:
        if not isinstance(error, CanvasError):
            return "Unexpected application error."
        message = str(error)
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        return message or "Canvas operation failed."

    def _outcome_record(
        self, state: _PreparedState, result: BatchRunResult, status: str
    ) -> dict[str, object]:
        prepared = state.approval
        rows: list[dict[str, str]] = []
        for file_snapshot in prepared.files:
            path = file_snapshot.path
            if result.submitted:
                file_status = "success"
                message = "Submitted."
            elif result.submission_uncertain:
                file_status = "verification_required"
                message = result.submission_error or "Verify in Canvas."
            elif result.submission_error:
                file_status = "submission_failed"
                message = result.submission_error
            elif path in result.upload_errors:
                file_status = "failed"
                message = result.upload_errors[path]
            else:
                file_status = "upload_ready"
                message = "Uploaded; no submission was created because another upload failed."
            rows.append(
                {"file_name": path.name, "status": file_status, "message": message}
            )
        log_written = True
        try:
            write_results_log(
                self._project_root / "exports",
                course_id=prepared.course.id,
                assignment_id=prepared.assignment.id,
                results=rows,
            )
        except OSError:
            log_written = False
        return {
            "batch_id": prepared.batch_id,
            "status": status,
            "submission_created": result.submitted,
            "retry_allowed": status == "upload_failed",
            "uploaded_files": [
                item.path.name
                for item in prepared.files
                if item.path in result.uploaded
            ],
            "failed_uploads": [
                {"file_name": path.name, "error": message}
                for path, message in result.upload_errors.items()
            ],
            "message": result.submission_error,
            "outcome_log_written": log_written,
        }

    def _review_record(self, prepared: PreparedApproval) -> dict[str, object]:
        return {
            "batch_id": prepared.batch_id,
            "created_at_utc": prepared.created_at_utc,
            "expires_in_seconds": self._ttl,
            "course": {
                "course_id": prepared.course.id,
                "course_name": prepared.course.name,
            },
            "assignment": self._assignment_record(prepared.assignment),
            "files": [
                {
                    "file_name": item.path.name,
                    "full_path": str(item.path),
                    "size_bytes": item.size,
                }
                for item in prepared.files
            ],
            "submission_created": False,
            "confirmation_required": f"SUBMIT {prepared.batch_id}",
        }

    @staticmethod
    def _assignment_record(assignment: CanvasAssignment) -> dict[str, object]:
        return {
            "assignment_id": assignment.id,
            "assignment_name": assignment.name,
            "submission_types": list(assignment.submission_types),
            "allowed_file_extensions": list(assignment.allowed_extensions),
            "accepts_online_upload": assignment.accepts_file_uploads,
            "already_submitted": assignment.is_submitted,
        }


def create_manager(
    client: CanvasClient,
    project_root: Path,
    *,
    submit_enabled: bool,
    api_key: str,
) -> PreparedBatchManager:
    """Construct the concrete manager while keeping the secret out of public state."""
    return PreparedBatchManager(
        client,
        project_root,
        submit_enabled=submit_enabled,
        secrets=(api_key,),
    )
