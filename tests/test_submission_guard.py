from __future__ import annotations

import unittest
from pathlib import Path

from app.canvas_client import CanvasClient, UploadedFile
from app.submission_guard import (
    ApprovalRequiredError,
    create_approval,
    require_matching_approval,
)


class SubmissionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = [Path("coursework/answer-one.pdf"), Path("coursework/answer-two.pdf")]

    def test_confirmation_is_required(self) -> None:
        with self.assertRaises(ApprovalRequiredError):
            create_approval(
                confirmed=False,
                course_id="100",
                assignment_id="200",
                file_paths=self.paths,
            )

    def test_changed_batch_invalidates_approval(self) -> None:
        approval = create_approval(
            confirmed=True,
            course_id="100",
            assignment_id="200",
            file_paths=self.paths,
        )
        with self.assertRaises(ApprovalRequiredError):
            require_matching_approval(
                approval,
                course_id="100",
                assignment_id="201",
                file_paths=self.paths,
            )

    def test_client_blocks_submission_before_any_request_without_approval(self) -> None:
        client = CanvasClient("https://school.example.edu", "not-a-real-token")
        try:
            with self.assertRaises(ApprovalRequiredError):
                client.create_submission(
                    "100",
                    "200",
                    [UploadedFile(self.paths[0].resolve(strict=False), "300")],
                    approval=None,
                )
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
