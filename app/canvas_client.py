"""Small Canvas HTTP client used by the desktop application."""

from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urljoin, urlsplit

import requests

from .config import url_has_same_origin, validate_canvas_base_url
from .submission_guard import ApprovalSnapshot, require_matching_approval


DEFAULT_TIMEOUT = (5, 30)
TRANSIENT_STATUS_CODES = {429, 502, 503, 504}


class CanvasError(RuntimeError):
    """Base class for safe, user-facing Canvas errors."""


class InvalidTokenError(CanvasError):
    """The API token is invalid or expired."""


class CanvasAccessError(CanvasError):
    """The requested Canvas object is not accessible."""


class CanvasNotFoundError(CanvasError):
    """The requested Canvas object does not exist or is hidden."""


class CanvasConnectionError(CanvasError):
    """Canvas could not be reached reliably."""


class UncertainSubmissionError(CanvasConnectionError):
    """The final submission request may have reached Canvas."""


class CanvasSecurityError(CanvasError):
    """Canvas supplied an unsafe redirect or URL."""


class CanvasAPIError(CanvasError):
    """Canvas returned an unexpected API response."""


class ExistingSubmissionError(CanvasError):
    """The assignment already has a submission and cannot be overwritten."""


class MissingLocalFileError(CanvasError):
    """A selected local file disappeared before upload."""


@dataclass(frozen=True)
class CanvasCourse:
    id: str
    name: str


@dataclass(frozen=True)
class CanvasAssignment:
    id: str
    name: str
    submission_types: tuple[str, ...]
    allowed_extensions: tuple[str, ...]

    @property
    def accepts_file_uploads(self) -> bool:
        return "online_upload" in self.submission_types


@dataclass(frozen=True)
class UploadedFile:
    path: Path
    canvas_file_id: str


def next_page_url(link_header: str, trusted_base_url: str) -> str | None:
    """Extract and validate the Canvas ``rel=next`` pagination URL."""
    for match in re.finditer(r"<([^>]+)>\s*((?:;[^,]*)*)", link_header):
        target, raw_parameters = match.groups()
        relations: list[str] = []
        for parameter in raw_parameters.split(";"):
            key, separator, value = parameter.strip().partition("=")
            if separator and key.lower() == "rel":
                relations.extend(value.strip().strip('"').split())
        if "next" not in relations:
            continue
        absolute_target = urljoin(trusted_base_url.rstrip("/") + "/", target)
        if not url_has_same_origin(absolute_target, trusted_base_url):
            raise CanvasSecurityError(
                "Canvas returned a pagination link on an unexpected host."
            )
        return absolute_target
    return None


class CanvasClient:
    """Canvas API access with strict origin, redirect, and retry behavior."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = validate_canvas_base_url(base_url)
        if not api_key:
            raise ValueError("An API key is required.")
        self._api_key = api_key
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "CanvasBatchUploader/1.0",
            }
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "CanvasClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get_active_courses(self) -> list[CanvasCourse]:
        pages = self._get_paginated(
            "/api/v1/courses",
            params={"enrollment_state": "active", "per_page": 100},
        )
        courses: list[CanvasCourse] = []
        for item in pages:
            if not isinstance(item, Mapping) or "id" not in item:
                raise CanvasAPIError("Canvas returned an invalid courses response.")
            name = str(item.get("name") or item.get("course_code") or f"Course {item['id']}")
            courses.append(CanvasCourse(str(item["id"]), name))
        return sorted(courses, key=lambda course: course.name.casefold())

    def get_assignments(self, course_id: str) -> list[CanvasAssignment]:
        course = quote(str(course_id), safe="")
        pages = self._get_paginated(
            f"/api/v1/courses/{course}/assignments",
            params={"per_page": 100},
        )
        assignments: list[CanvasAssignment] = []
        for item in pages:
            if not isinstance(item, Mapping) or "id" not in item:
                raise CanvasAPIError("Canvas returned an invalid assignments response.")
            submission_types = item.get("submission_types") or []
            allowed_extensions = item.get("allowed_extensions") or []
            assignments.append(
                CanvasAssignment(
                    str(item["id"]),
                    str(item.get("name") or f"Assignment {item['id']}"),
                    tuple(str(value) for value in submission_types),
                    tuple(str(value).lstrip(".").lower() for value in allowed_extensions),
                )
            )
        return sorted(assignments, key=lambda assignment: assignment.name.casefold())

    def ensure_assignment_is_unsubmitted(
        self, course_id: str, assignment_id: str
    ) -> None:
        course = quote(str(course_id), safe="")
        assignment = quote(str(assignment_id), safe="")
        response = self._canvas_request(
            "GET",
            f"/api/v1/courses/{course}/assignments/{assignment}/submissions/self",
            retry_transient=True,
        )
        payload = self._json_object(response, "submission")
        workflow_state = str(payload.get("workflow_state") or "").lower()
        attachments = payload.get("attachments") or []
        try:
            attempt = int(payload.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if (
            payload.get("submitted_at")
            or payload.get("submission_type")
            or attachments
            or attempt > 0
            or workflow_state in {"submitted", "graded", "pending_review"}
        ):
            raise ExistingSubmissionError(
                "This assignment already has a submission. The app will not replace it."
            )

    def upload_file(
        self, course_id: str, assignment_id: str, path: Path
    ) -> UploadedFile:
        """Upload one local file but do not create a Canvas submission."""
        path = path.resolve(strict=False)
        if not path.is_file():
            raise MissingLocalFileError(f"Local file is missing: {path.name}")

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        course = quote(str(course_id), safe="")
        assignment = quote(str(assignment_id), safe="")
        response = self._canvas_request(
            "POST",
            f"/api/v1/courses/{course}/assignments/{assignment}/submissions/self/files",
            data={
                "name": path.name,
                "size": path.stat().st_size,
                "content_type": content_type,
            },
        )
        upload_details = self._json_object(response, "file upload initiation")
        upload_url = upload_details.get("upload_url")
        upload_params = upload_details.get("upload_params")
        if not isinstance(upload_url, str) or not isinstance(upload_params, Mapping):
            raise CanvasAPIError("Canvas returned invalid file upload instructions.")
        self._require_safe_https_url(upload_url, "upload")

        normalized_params: dict[str, object] = {}
        for key, value in upload_params.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
                raise CanvasAPIError("Canvas returned invalid file upload parameters.")
            normalized_params[key] = value

        uploaded_payload = self._post_upload(
            upload_url,
            normalized_params,
            path,
            content_type,
        )
        file_record: Mapping[str, Any] = uploaded_payload
        attachment = uploaded_payload.get("attachment")
        if isinstance(attachment, Mapping):
            file_record = attachment
        file_id = file_record.get("id")
        if file_id is None:
            raise CanvasAPIError("Canvas did not return an ID for the uploaded file.")
        return UploadedFile(path, str(file_id))

    def create_submission(
        self,
        course_id: str,
        assignment_id: str,
        uploaded_files: Iterable[UploadedFile],
        *,
        approval: ApprovalSnapshot | None,
    ) -> Mapping[str, Any]:
        """Create one submission from uploaded files after a final overwrite check."""
        uploaded = tuple(uploaded_files)
        if not uploaded:
            raise CanvasAPIError("No uploaded files were supplied for submission.")
        require_matching_approval(
            approval,
            course_id=str(course_id),
            assignment_id=str(assignment_id),
            file_paths=(uploaded_file.path for uploaded_file in uploaded),
        )
        self.ensure_assignment_is_unsubmitted(course_id, assignment_id)
        course = quote(str(course_id), safe="")
        assignment = quote(str(assignment_id), safe="")
        form_data: list[tuple[str, str]] = [
            ("submission[submission_type]", "online_upload")
        ]
        form_data.extend(
            ("submission[file_ids][]", uploaded_file.canvas_file_id)
            for uploaded_file in uploaded
        )
        try:
            response = self._canvas_request(
                "POST",
                f"/api/v1/courses/{course}/assignments/{assignment}/submissions",
                data=form_data,
            )
        except CanvasConnectionError as error:
            raise UncertainSubmissionError(
                "The final submission status is uncertain because the connection failed. "
                "Do not retry; verify the assignment in Canvas."
            ) from error
        return self._json_object(response, "submission creation")

    def _get_paginated(
        self, path: str, *, params: Mapping[str, object]
    ) -> list[object]:
        url: str | None = self._canvas_url(path)
        first_request = True
        items: list[object] = []
        while url:
            response = self._canvas_request(
                "GET",
                url,
                params=params if first_request else None,
                retry_transient=True,
            )
            first_request = False
            payload = self._json_value(response, "paginated list")
            if not isinstance(payload, list):
                raise CanvasAPIError("Canvas returned an invalid paginated response.")
            items.extend(payload)
            url = next_page_url(response.headers.get("Link", ""), self.base_url)
        return items

    def _canvas_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("/"):
            candidate = self.base_url + path_or_url
        else:
            candidate = path_or_url
        if not url_has_same_origin(candidate, self.base_url):
            raise CanvasSecurityError("Refusing to send authorization outside Canvas.")
        return candidate

    def _canvas_request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, object] | None = None,
        data: object = None,
        retry_transient: bool = False,
    ) -> requests.Response:
        url = self._canvas_url(path_or_url)
        attempts = 3 if retry_transient else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise CanvasConnectionError(
                    "Could not connect to Canvas. Check your network and try again."
                ) from error

            if 300 <= response.status_code < 400:
                response.close()
                raise CanvasSecurityError(
                    "Canvas attempted an HTTP redirect; it was blocked for safety."
                )
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
                response.close()
                time.sleep(self._retry_delay(response, attempt))
                continue
            self._raise_for_status(response)
            return response
        raise CanvasConnectionError("Canvas did not respond after several attempts.")

    def _post_upload(
        self,
        upload_url: str,
        upload_params: Mapping[str, object],
        path: Path,
        content_type: str,
    ) -> Mapping[str, Any]:
        for attempt in range(1, 3):
            try:
                with path.open("rb") as file_handle:
                    response = self._session.post(
                        upload_url,
                        data=upload_params,
                        files={"file": (path.name, file_handle, content_type)},
                        headers={"Authorization": None},
                        timeout=self._timeout,
                        allow_redirects=False,
                    )
            except FileNotFoundError as error:
                raise MissingLocalFileError(f"Local file is missing: {path.name}") from error
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt < 2:
                    time.sleep(0.5)
                    continue
                raise CanvasConnectionError(
                    f"The upload connection failed for {path.name}."
                ) from error

            if response.status_code in TRANSIENT_STATUS_CODES and attempt < 2:
                response.close()
                time.sleep(self._retry_delay(response, attempt))
                continue
            location = response.headers.get("Location")
            if 300 <= response.status_code < 400 or (
                response.status_code == 201 and location
            ):
                response.close()
                if not location:
                    raise CanvasSecurityError(
                        "The upload returned an unexpected redirect."
                    )
                redirect_target = urljoin(upload_url, location)
                if not url_has_same_origin(redirect_target, self.base_url):
                    raise CanvasSecurityError(
                        "The upload completion redirect did not return to the "
                        "configured Canvas host."
                    )
                completion = self._canvas_request(
                    "GET", redirect_target, retry_transient=True
                )
                return self._json_object(completion, "file upload completion")
            self._raise_for_status(response)
            return self._json_object(response, "file upload")
        raise CanvasConnectionError(f"The upload failed for {path.name}.")

    @staticmethod
    def _require_safe_https_url(url: str, label: str) -> None:
        parsed = urlsplit(url)
        try:
            parsed.port
        except ValueError as error:
            raise CanvasSecurityError(
                f"Canvas returned an unsafe {label} URL."
            ) from error
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise CanvasSecurityError(f"Canvas returned an unsafe {label} URL.")

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(max(float(retry_after), 0.0), 2.0)
        except ValueError:
            return 0.5 * attempt

    @staticmethod
    def _json_value(response: requests.Response, context: str) -> Any:
        try:
            return response.json()
        except requests.JSONDecodeError as error:
            raise CanvasAPIError(
                f"Canvas returned invalid JSON during {context}."
            ) from error

    @classmethod
    def _json_object(
        cls, response: requests.Response, context: str
    ) -> Mapping[str, Any]:
        payload = cls._json_value(response, context)
        if not isinstance(payload, Mapping):
            raise CanvasAPIError(f"Canvas returned an invalid {context} response.")
        return payload

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        response.close()
        if status == 401:
            raise InvalidTokenError("The Canvas API token is invalid or expired.")
        if status == 403:
            raise CanvasAccessError(
                "Canvas denied access. Check the selected course or assignment."
            )
        if status == 404:
            raise CanvasNotFoundError(
                "The selected Canvas course or assignment is not accessible."
            )
        if status in TRANSIENT_STATUS_CODES:
            raise CanvasConnectionError(
                f"Canvas is temporarily unavailable (HTTP {status}). Try again."
            )
        raise CanvasAPIError(f"Canvas rejected the request (HTTP {status}).")
