"""Headless stdio MCP server for the Canvas Batch Uploader."""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path
from typing import Callable, Protocol, TypeVar
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .batch_service import (
    BatchServiceError,
    PreparedBatchManager,
    create_manager,
    list_local_files,
)
from .canvas_client import CanvasClient, CanvasError
from .config import (
    AppConfig,
    ConfigurationError,
    load_env_file,
    validate_canvas_base_url,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
T = TypeVar("T")


SERVER_INSTRUCTIONS = (
    "Canvas uploads and submissions are disabled unless the server was explicitly "
    "launched with --enable-submit. "
    "Assignment-file downloads write only to an explicitly named existing local folder. "
    "Prepare a batch before submitting it, show the returned review to the user, and pass "
    "the exact confirmation string returned by the prepare tool. Never retry an uncertain "
    "final submission; verify it in Canvas. Approval snapshots expire after 10 minutes."
)


READ_EXTERNAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
READ_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
PREPARE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
DOWNLOAD_LOCAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
DESTRUCTIVE_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


class ToolRuntime(Protocol):
    """Operations exposed to the MCP registration layer."""

    def configuration_status(self) -> dict[str, object]: ...

    def list_active_courses(self) -> list[dict[str, str]]: ...

    def list_assignments(self, course_id: str) -> list[dict[str, object]]: ...

    def list_assignment_files(
        self, course_id: str, assignment_id: str
    ) -> list[dict[str, object]]: ...

    def download_assignment_files(
        self, course_id: str, assignment_id: str, destination_folder: str
    ) -> dict[str, object]: ...

    def list_local_files(self, folder_path: str) -> list[dict[str, object]]: ...

    def prepare_batch(
        self, course_id: str, assignment_id: str, file_paths: list[str]
    ) -> dict[str, object]: ...

    def submit_prepared_batch(
        self, batch_id: str, confirmation: str
    ) -> dict[str, object]: ...

    def retry_failed_uploads(
        self, batch_id: str, confirmation: str
    ) -> dict[str, object]: ...

    def safe_call(self, action: Callable[[], T]) -> T: ...


class CanvasMCPRuntime:
    """Lazy configuration and client state for one stdio server process."""

    def __init__(self, project_root: Path, *, submit_enabled: bool = False) -> None:
        self.project_root = project_root
        self.submit_enabled = submit_enabled
        self._manager: PreparedBatchManager | None = None
        self._api_key = ""
        self._lock = threading.RLock()

    def configuration_status(self) -> dict[str, object]:
        load_env_file(self.project_root / ".env")
        token = os.getenv("API_KEY", "").strip()
        raw_url = os.getenv("CANVAS_BASE_URL", "")
        result: dict[str, object] = {
            "api_key_present": bool(token),
            "canvas_base_url_present": bool(raw_url.strip()),
            "canvas_url_valid": False,
            "canvas_host": None,
            "configured": False,
        }
        if raw_url.strip():
            try:
                normalized = validate_canvas_base_url(raw_url)
            except ConfigurationError as error:
                result["configuration_error"] = str(error)
            else:
                result["canvas_url_valid"] = True
                result["canvas_host"] = urlsplit(normalized).netloc
        result["configured"] = bool(token) and bool(result["canvas_url_valid"])
        return result

    def list_active_courses(self) -> list[dict[str, str]]:
        return self._get_manager().list_active_courses()

    def list_assignments(self, course_id: str) -> list[dict[str, object]]:
        return self._get_manager().list_assignments(course_id)

    def list_assignment_files(
        self, course_id: str, assignment_id: str
    ) -> list[dict[str, object]]:
        return self._get_manager().list_assignment_files(course_id, assignment_id)

    def download_assignment_files(
        self, course_id: str, assignment_id: str, destination_folder: str
    ) -> dict[str, object]:
        return self._get_manager().download_assignment_files(
            course_id, assignment_id, destination_folder
        )

    @staticmethod
    def list_local_files(folder_path: str) -> list[dict[str, object]]:
        return list_local_files(folder_path)

    def prepare_batch(
        self, course_id: str, assignment_id: str, file_paths: list[str]
    ) -> dict[str, object]:
        return self._get_manager().prepare(course_id, assignment_id, file_paths)

    def submit_prepared_batch(
        self, batch_id: str, confirmation: str
    ) -> dict[str, object]:
        return self._get_manager().submit(batch_id, confirmation)

    def retry_failed_uploads(
        self, batch_id: str, confirmation: str
    ) -> dict[str, object]:
        return self._get_manager().retry_failed(batch_id, confirmation)

    def safe_call(self, action: Callable[[], T]) -> T:
        try:
            return action()
        except (BatchServiceError, CanvasError, ConfigurationError) as error:
            raise ValueError(self._redact(str(error))) from None
        except Exception:
            raise RuntimeError("Unexpected application error.") from None

    def close(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None
            self._api_key = ""

    def _get_manager(self) -> PreparedBatchManager:
        with self._lock:
            if self._manager is None:
                config = AppConfig.from_project(self.project_root)
                self._api_key = config.api_key
                self._manager = create_manager(
                    CanvasClient(config.canvas_base_url, config.api_key),
                    self.project_root,
                    submit_enabled=self.submit_enabled,
                    api_key=config.api_key,
                )
            return self._manager

    def _redact(self, message: str) -> str:
        secrets = {self._api_key, os.getenv("API_KEY", "").strip()}
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message or "Canvas operation failed."


def create_mcp_server(runtime: ToolRuntime) -> MCPServer:
    """Register all tools against a runtime without loading configuration."""
    server = MCPServer(
        "canvas-batch-uploader",
        title="Canvas Batch Uploader",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(annotations=READ_LOCAL)
    def canvas_configuration_status() -> dict[str, object]:
        """Check required configuration and return only the normalized Canvas host."""
        return runtime.safe_call(runtime.configuration_status)

    @server.tool(annotations=READ_EXTERNAL)
    def canvas_list_active_courses() -> list[dict[str, str]]:
        """List active Canvas course IDs and names."""
        return runtime.safe_call(runtime.list_active_courses)

    @server.tool(annotations=READ_EXTERNAL)
    def canvas_list_assignments(course_id: str) -> list[dict[str, object]]:
        """List assignments, submission types, extensions, and upload support for a course."""
        return runtime.safe_call(lambda: runtime.list_assignments(course_id))

    @server.tool(annotations=READ_EXTERNAL)
    def canvas_list_assignment_files(
        course_id: str, assignment_id: str
    ) -> list[dict[str, object]]:
        """List Canvas-hosted files linked from one assignment without downloading."""
        return runtime.safe_call(
            lambda: runtime.list_assignment_files(course_id, assignment_id)
        )

    @server.tool(annotations=DOWNLOAD_LOCAL)
    def canvas_download_assignment_files(
        course_id: str, assignment_id: str, destination_folder: str
    ) -> dict[str, object]:
        """Download assignment files into an existing absolute folder without overwriting."""
        return runtime.safe_call(
            lambda: runtime.download_assignment_files(
                course_id, assignment_id, destination_folder
            )
        )

    @server.tool(annotations=READ_LOCAL)
    def canvas_list_local_files(folder_path: str) -> list[dict[str, object]]:
        """List safe regular files directly inside an absolute folder without reading contents."""
        return runtime.safe_call(lambda: runtime.list_local_files(folder_path))

    @server.tool(annotations=PREPARE)
    def canvas_prepare_batch(
        course_id: str, assignment_id: str, file_paths: list[str]
    ) -> dict[str, object]:
        """Validate exact absolute files and create a 10-minute in-memory review; never upload."""
        return runtime.safe_call(
            lambda: runtime.prepare_batch(course_id, assignment_id, file_paths)
        )

    @server.tool(annotations=DESTRUCTIVE_WRITE)
    def canvas_submit_prepared_batch(
        batch_id: str, confirmation: str
    ) -> dict[str, object]:
        """Upload and submit one prepared batch after write mode and exact confirmation checks."""
        return runtime.safe_call(
            lambda: runtime.submit_prepared_batch(batch_id, confirmation)
        )

    @server.tool(annotations=DESTRUCTIVE_WRITE)
    def canvas_retry_failed_uploads(
        batch_id: str, confirmation: str
    ) -> dict[str, object]:
        """Retry only failed uploads for an approved unsubmitted batch, then submit once."""
        return runtime.safe_call(
            lambda: runtime.retry_failed_uploads(batch_id, confirmation)
        )

    return server


runtime = CanvasMCPRuntime(PROJECT_ROOT)
mcp = create_mcp_server(runtime)


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server over stdio, enabling writes only by explicit flag."""
    parser = argparse.ArgumentParser(description="Canvas Batch Uploader MCP server")
    parser.add_argument(
        "--enable-submit",
        action="store_true",
        help="enable confirmed Canvas upload and submission tools",
    )
    args = parser.parse_args(argv)
    runtime.submit_enabled = args.enable_submit
    try:
        mcp.run("stdio")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
