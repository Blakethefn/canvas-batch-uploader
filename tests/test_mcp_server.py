from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, TypeVar
from unittest.mock import patch

from mcp import Client

from app.canvas_client import CanvasError
from app.mcp_server import CanvasMCPRuntime, create_mcp_server


T = TypeVar("T")


class FakeRuntime:
    def configuration_status(self) -> dict[str, object]:
        return {"configured": False}

    def list_active_courses(self) -> list[dict[str, str]]:
        return []

    def list_assignments(self, _course_id: str) -> list[dict[str, object]]:
        return []

    def list_assignment_files(
        self, _course_id: str, _assignment_id: str
    ) -> list[dict[str, object]]:
        return []

    def download_assignment_files(
        self, _course_id: str, _assignment_id: str, _destination_folder: str
    ) -> dict[str, object]:
        return {"downloaded_count": 0}

    def list_local_files(self, _folder_path: str) -> list[dict[str, object]]:
        return []

    def prepare_batch(
        self, _course_id: str, _assignment_id: str, _file_paths: list[str]
    ) -> dict[str, object]:
        return {"batch_id": "offline"}

    def submit_prepared_batch(
        self, _batch_id: str, _confirmation: str
    ) -> dict[str, object]:
        return {"status": "blocked"}

    def retry_failed_uploads(
        self, _batch_id: str, _confirmation: str
    ) -> dict[str, object]:
        return {"status": "blocked"}

    @staticmethod
    def safe_call(action: Callable[[], T]) -> T:
        return action()


class MCPServerTests(unittest.TestCase):
    def test_all_tools_are_registered_with_write_annotations(self) -> None:
        async def inspect_tools() -> None:
            async with Client(create_mcp_server(FakeRuntime())) as client:
                listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            self.assertEqual(
                set(tools),
                {
                    "canvas_configuration_status",
                    "canvas_list_active_courses",
                    "canvas_list_assignments",
                    "canvas_list_assignment_files",
                    "canvas_download_assignment_files",
                    "canvas_list_local_files",
                    "canvas_prepare_batch",
                    "canvas_submit_prepared_batch",
                    "canvas_retry_failed_uploads",
                },
            )
            self.assertTrue(tools["canvas_list_active_courses"].annotations.read_only_hint)
            download = tools["canvas_download_assignment_files"].annotations
            self.assertFalse(download.read_only_hint)
            self.assertFalse(download.destructive_hint)
            self.assertFalse(download.idempotent_hint)
            submit = tools["canvas_submit_prepared_batch"].annotations
            retry = tools["canvas_retry_failed_uploads"].annotations
            self.assertFalse(submit.read_only_hint)
            self.assertTrue(submit.destructive_hint)
            self.assertFalse(submit.idempotent_hint)
            self.assertTrue(retry.destructive_hint)

        asyncio.run(inspect_tools())

    def test_token_is_redacted_from_safe_errors_and_status(self) -> None:
        secret = "super-secret-canvas-token"
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = CanvasMCPRuntime(Path(temporary_directory))
            with patch.dict(
                os.environ,
                {
                    "API_KEY": secret,
                    "CANVAS_BASE_URL": "https://school.example.edu",
                },
                clear=False,
            ):
                status = runtime.configuration_status()
                with self.assertRaises(ValueError) as caught:
                    runtime.safe_call(
                        lambda: (_ for _ in ()).throw(CanvasError(f"failure {secret}"))
                    )
            self.assertNotIn(secret, repr(status))
            self.assertNotIn(secret, str(caught.exception))
            self.assertIn("[REDACTED]", str(caught.exception))

    def test_headless_import_and_stdio_eof_write_nothing_to_stdout(self) -> None:
        environment = os.environ.copy()
        environment.pop("DISPLAY", None)
        import_check = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import app.mcp_server; assert 'tkinter' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(import_check.returncode, 0, import_check.stderr)
        self.assertEqual(import_check.stdout, "")

        server_check = subprocess.run(
            [sys.executable, "-m", "app.mcp_server"],
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            input="",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(server_check.returncode, 0, server_check.stderr)
        self.assertEqual(server_check.stdout, "")


if __name__ == "__main__":
    unittest.main()
