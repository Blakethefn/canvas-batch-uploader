from __future__ import annotations

import unittest
from typing import Any

from app.canvas_client import CanvasClient, submission_exists


class FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def close(self) -> None:
        pass


class AssignmentSession:
    def __init__(self, payload: object) -> None:
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse(self.payload)

    def close(self) -> None:
        pass


class AssignmentStatusTests(unittest.TestCase):
    def test_detects_real_submission_attempts(self) -> None:
        submitted_records = (
            {"submitted_at": "2026-08-15T12:00:00Z"},
            {"submission_type": "online_upload"},
            {"attachments": [{"id": 1}]},
            {"attempt": "1"},
            {"workflow_state": "graded"},
        )
        for record in submitted_records:
            with self.subTest(record=record):
                self.assertTrue(submission_exists(record))

        self.assertFalse(
            submission_exists(
                {"workflow_state": "unsubmitted", "attempt": 0, "attachments": []}
            )
        )

    def test_assignment_list_includes_and_reads_submission_status(self) -> None:
        session = AssignmentSession(
            [
                {
                    "id": 10,
                    "name": "Finished work",
                    "submission_types": ["online_upload"],
                    "submission": {"workflow_state": "submitted", "attempt": 1},
                },
                {
                    "id": 11,
                    "name": "New work",
                    "submission_types": ["online_upload"],
                    "submission": {"workflow_state": "unsubmitted", "attempt": 0},
                },
            ]
        )
        client = CanvasClient(
            "https://school.example.edu",
            "not-a-real-token",
            session=session,  # type: ignore[arg-type]
        )

        assignments = client.get_assignments("100")

        by_id = {assignment.id: assignment for assignment in assignments}
        self.assertTrue(by_id["10"].is_submitted)
        self.assertFalse(by_id["11"].is_submitted)
        self.assertEqual(
            session.requests[0]["params"],
            {"per_page": 100, "include[]": "submission"},
        )


if __name__ == "__main__":
    unittest.main()
