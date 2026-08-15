from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.canvas_client import CanvasClient, CanvasSecurityError


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: object = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def close(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, upload_response: FakeResponse) -> None:
        self.headers: dict[str, str] = {}
        self.upload_response = upload_response
        self.upload_headers: dict[str, Any] = {}
        self.canvas_requests: list[dict[str, Any]] = []

    def post(self, _url: str, **kwargs: Any) -> FakeResponse:
        self.upload_headers = kwargs.get("headers", {})
        return self.upload_response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.canvas_requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse(200, payload={"id": 321})

    def close(self) -> None:
        pass


class UploadRedirectTests(unittest.TestCase):
    def _temporary_file(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        path = Path(temporary_directory.name) / "coursework.pdf"
        path.write_bytes(b"offline test")
        return temporary_directory, path

    def test_rejects_upload_completion_on_another_host(self) -> None:
        temporary_directory, path = self._temporary_file()
        self.addCleanup(temporary_directory.cleanup)
        session = FakeSession(
            FakeResponse(302, headers={"Location": "https://attacker.example/complete"})
        )
        client = CanvasClient(
            "https://school.example.edu", "not-a-real-token", session=session  # type: ignore[arg-type]
        )
        with self.assertRaises(CanvasSecurityError):
            client._post_upload(  # noqa: SLF001 - focused security boundary test
                "https://uploads.example.net/presigned",
                {"opaque": "value"},
                path,
                "application/pdf",
            )

    def test_completes_documented_location_on_canvas_host(self) -> None:
        temporary_directory, path = self._temporary_file()
        self.addCleanup(temporary_directory.cleanup)
        completion_url = "https://school.example.edu/api/v1/files/321/create_success"
        session = FakeSession(FakeResponse(201, headers={"Location": completion_url}))
        client = CanvasClient(
            "https://school.example.edu", "not-a-real-token", session=session  # type: ignore[arg-type]
        )
        payload = client._post_upload(  # noqa: SLF001 - focused workflow test
            "https://uploads.example.net/presigned",
            {"opaque": "value"},
            path,
            "application/pdf",
        )
        self.assertEqual(payload["id"], 321)
        self.assertIsNone(session.upload_headers["Authorization"])
        self.assertEqual(session.canvas_requests[0]["url"], completion_url)
        self.assertEqual(
            session.canvas_requests[0]["headers"]["Authorization"],
            "Bearer not-a-real-token",
        )


if __name__ == "__main__":
    unittest.main()
