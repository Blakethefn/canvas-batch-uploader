from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Iterable, Mapping

from app.batch_service import (
    BatchChangedError,
    BatchExpiredError,
    BatchServiceError,
    PreparedBatchManager,
    SubmissionDisabledError,
    list_local_files,
)
from app.canvas_client import (
    CanvasAssignment,
    CanvasCourse,
    CanvasError,
    UncertainSubmissionError,
    UploadedFile,
)
from app.submission_guard import ApprovalSnapshot


class FakeCanvasClient:
    def __init__(self) -> None:
        self.course = CanvasCourse("100", "Offline Course")
        self.assignment = CanvasAssignment(
            "200", "Offline Assignment", ("online_upload",), ("pdf", "txt")
        )
        self.failed_names: set[str] = set()
        self.uncertain_submission = False
        self.upload_calls: list[str] = []
        self.submission_calls = 0
        self.unsubmitted_checks = 0
        self.closed = False

    def get_active_courses(self) -> list[CanvasCourse]:
        return [self.course]

    def get_assignments(self, _course_id: str) -> list[CanvasAssignment]:
        return [self.assignment]

    def ensure_assignment_is_unsubmitted(
        self, _course_id: str, _assignment_id: str
    ) -> None:
        self.unsubmitted_checks += 1

    def upload_file(
        self, _course_id: str, _assignment_id: str, path: Path
    ) -> UploadedFile:
        self.upload_calls.append(path.name)
        if path.name in self.failed_names:
            raise CanvasError(f"Upload failed for {path.name}.")
        return UploadedFile(path, f"file-{path.name}")

    def create_submission(
        self,
        _course_id: str,
        _assignment_id: str,
        _uploaded_files: Iterable[UploadedFile],
        *,
        approval: ApprovalSnapshot | None,
    ) -> Mapping[str, object]:
        self.submission_calls += 1
        if self.uncertain_submission:
            raise UncertainSubmissionError("Final status uncertain.")
        return {"id": "submission-1"}

    def close(self) -> None:
        self.closed = True


class PreparedBatchManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.first = self.root / "answer.pdf"
        self.second = self.root / "notes.txt"
        self.first.write_bytes(b"first answer")
        self.second.write_bytes(b"supporting notes")
        self.client = FakeCanvasClient()

    def _manager(
        self,
        *,
        submit_enabled: bool = True,
        clock=lambda: 0.0,  # noqa: B008 - deterministic test clock
        ttl: int = 600,
    ) -> PreparedBatchManager:
        manager = PreparedBatchManager(
            self.client,
            self.root,
            submit_enabled=submit_enabled,
            clock=clock,
            approval_ttl_seconds=ttl,
        )
        self.addCleanup(manager.close)
        return manager

    def _prepare(
        self, manager: PreparedBatchManager, paths: list[Path] | None = None
    ) -> dict[str, object]:
        return manager.prepare(
            "100", "200", [str(path) for path in (paths or [self.first])]
        )

    def test_submission_is_blocked_by_default(self) -> None:
        manager = self._manager(submit_enabled=False)
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        with self.assertRaises(SubmissionDisabledError):
            manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(self.client.upload_calls, [])
        self.assertEqual(self.client.submission_calls, 0)

    def test_exact_confirmation_is_required(self) -> None:
        manager = self._manager()
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        with self.assertRaisesRegex(BatchServiceError, "Exact confirmation"):
            manager.submit(batch_id, f"submit {batch_id}")
        self.assertEqual(self.client.upload_calls, [])

    def test_changed_file_invalidates_approval(self) -> None:
        manager = self._manager()
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        self.first.write_bytes(b"changed after approval")
        with self.assertRaises(BatchChangedError):
            manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(self.client.upload_calls, [])

    def test_expired_approval_is_rejected(self) -> None:
        now = [0.0]
        manager = self._manager(clock=lambda: now[0], ttl=5)
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        now[0] = 6.0
        with self.assertRaises(BatchExpiredError):
            manager.submit(batch_id, f"SUBMIT {batch_id}")

    def test_success_consumes_approval_once(self) -> None:
        manager = self._manager()
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        result = manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(result["status"], "submitted")
        with self.assertRaisesRegex(BatchServiceError, "already used"):
            manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(self.client.submission_calls, 1)

    def test_no_submission_when_any_upload_fails_and_retry_only_retries_failure(self) -> None:
        manager = self._manager()
        self.client.failed_names.add(self.second.name)
        review = self._prepare(manager, [self.first, self.second])
        batch_id = str(review["batch_id"])
        result = manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(result["status"], "upload_failed")
        self.assertEqual(self.client.submission_calls, 0)
        self.assertEqual(self.client.upload_calls, [self.first.name, self.second.name])

        self.client.failed_names.clear()
        retry_result = manager.retry_failed(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(retry_result["status"], "submitted")
        self.assertEqual(
            self.client.upload_calls,
            [self.first.name, self.second.name, self.second.name],
        )
        self.assertEqual(self.client.submission_calls, 1)

    def test_uncertain_submission_can_never_be_retried(self) -> None:
        manager = self._manager()
        self.client.uncertain_submission = True
        review = self._prepare(manager)
        batch_id = str(review["batch_id"])
        result = manager.submit(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(result["status"], "submission_uncertain")
        with self.assertRaisesRegex(BatchServiceError, "Do not retry"):
            manager.retry_failed(batch_id, f"SUBMIT {batch_id}")
        self.assertEqual(self.client.submission_calls, 1)

    def test_local_listing_excludes_credentials_and_does_not_recurse(self) -> None:
        (self.root / ".env").write_text("API_KEY=never-return-this", encoding="utf-8")
        (self.root / "canvas-credentials.json").write_text("secret", encoding="utf-8")
        (self.root / "private.key").write_text("secret", encoding="utf-8")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "nested.pdf").write_bytes(b"nested")
        records = list_local_files(str(self.root.resolve()))
        names = {str(record["file_name"]) for record in records}
        self.assertEqual(names, {self.first.name, self.second.name})
        for record in records:
            self.assertNotIn("contents", record)


if __name__ == "__main__":
    unittest.main()
