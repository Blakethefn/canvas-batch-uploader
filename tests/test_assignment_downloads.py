from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from app.canvas_client import CanvasClient, CanvasConnectionError, CanvasSecurityError


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: object = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.body = body

    def json(self) -> object:
        return self._payload

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [
            self.body[index : index + chunk_size]
            for index in range(0, len(self.body), chunk_size)
        ]

    def close(self) -> None:
        pass


class RoutingSession:
    def __init__(
        self, route: Callable[[str, str, dict[str, Any]], FakeResponse]
    ) -> None:
        self.headers: dict[str, str] = {}
        self.route = route
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.route(method, url, kwargs)

    def close(self) -> None:
        pass


class AssignmentDownloadTests(unittest.TestCase):
    def test_discovers_deduplicated_canvas_file_references(self) -> None:
        def route(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
            if url.endswith("/assignments/200"):
                return FakeResponse(
                    payload={
                        "attachments": [{"id": 56}],
                        "annotatable_attachment_id": 55,
                        "description": (
                            '<a href="/courses/100/files/55/download" '
                            'data-api-endpoint="/api/v1/files/55" '
                            'data-api-returntype="File">Worksheet</a>'
                            '<a href="/courses/100/files/57">Notes</a>'
                            '<a href="https://attacker.example/files/99">Ignore</a>'
                        ),
                    }
                )
            file_id = url.rsplit("/", 1)[-1]
            names = {"55": "worksheet.pdf", "56": "appendix.txt", "57": "notes.docx"}
            return FakeResponse(
                payload={
                    "id": file_id,
                    "display_name": names[file_id],
                    "size": 123,
                    "url": f"https://school.example.edu/files/{file_id}/download",
                }
            )

        session = RoutingSession(route)
        client = CanvasClient(
            "https://school.example.edu",
            "not-a-real-token",
            session=session,  # type: ignore[arg-type]
        )

        files = client.get_assignment_files("100", "200")

        self.assertEqual([item.id for item in files], ["56", "57", "55"])
        metadata_urls = [
            request["url"]
            for request in session.requests
            if "/api/v1/files/" in request["url"]
        ]
        self.assertEqual(len(metadata_urls), 3)

    def test_download_follows_https_redirect_without_forwarding_token(self) -> None:
        canvas_download = "https://school.example.edu/files/55/download?verifier=opaque"
        cdn_download = "https://cdn.example.net/signed-download"

        def route(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
            if url.endswith("/assignments/200"):
                return FakeResponse(payload={"annotatable_attachment_id": 55})
            if url.endswith("/api/v1/files/55"):
                return FakeResponse(
                    payload={
                        "id": 55,
                        "display_name": "worksheet.pdf",
                        "size": 11,
                        "url": canvas_download,
                    }
                )
            if url == canvas_download:
                return FakeResponse(302, headers={"Location": cdn_download})
            if url == cdn_download:
                return FakeResponse(body=b"new content")
            raise AssertionError(f"Unexpected URL: {url}")

        session = RoutingSession(route)
        client = CanvasClient(
            "https://school.example.edu",
            "not-a-real-token",
            session=session,  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            existing = folder / "worksheet.pdf"
            existing.write_bytes(b"keep this")

            downloaded = client.download_assignment_files("100", "200", folder)

            self.assertEqual(downloaded, [folder / "worksheet (2).pdf"])
            self.assertEqual(existing.read_bytes(), b"keep this")
            self.assertEqual(downloaded[0].read_bytes(), b"new content")

        download_requests = {
            request["url"]: request for request in session.requests if request.get("stream")
        }
        self.assertEqual(
            download_requests[canvas_download]["headers"]["Authorization"],
            "Bearer not-a-real-token",
        )
        self.assertIsNone(
            download_requests[cdn_download]["headers"]["Authorization"]
        )

    def test_rejects_insecure_download_redirect(self) -> None:
        def route(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
            if url.endswith("/assignments/200"):
                return FakeResponse(payload={"annotatable_attachment_id": 55})
            if url.endswith("/api/v1/files/55"):
                return FakeResponse(
                    payload={
                        "id": 55,
                        "display_name": "worksheet.pdf",
                        "url": "https://school.example.edu/files/55/download",
                    }
                )
            return FakeResponse(
                302, headers={"Location": "http://cdn.example.net/unsafe"}
            )

        client = CanvasClient(
            "https://school.example.edu",
            "not-a-real-token",
            session=RoutingSession(route),  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(CanvasSecurityError):
                client.download_assignment_files(
                    "100", "200", Path(temporary_directory)
                )
            self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_removes_incomplete_download(self) -> None:
        def route(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
            if url.endswith("/assignments/200"):
                return FakeResponse(payload={"annotatable_attachment_id": 55})
            if url.endswith("/api/v1/files/55"):
                return FakeResponse(
                    payload={
                        "id": 55,
                        "display_name": "worksheet.pdf",
                        "size": 100,
                        "url": "https://school.example.edu/files/55/download",
                    }
                )
            return FakeResponse(body=b"short")

        client = CanvasClient(
            "https://school.example.edu",
            "not-a-real-token",
            session=RoutingSession(route),  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            with self.assertRaisesRegex(CanvasConnectionError, "incomplete"):
                client.download_assignment_files("100", "200", folder)
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
