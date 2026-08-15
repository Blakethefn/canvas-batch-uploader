"""Pure approval checks shared by the UI and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ApprovalRequiredError(ValueError):
    """Raised when the current batch has not been explicitly approved."""


def _normalized_paths(paths: Iterable[Path]) -> tuple[str, ...]:
    return tuple(str(path.resolve(strict=False)) for path in paths)


@dataclass(frozen=True)
class ApprovalSnapshot:
    """The exact files and target that a user approved."""

    course_id: str
    assignment_id: str
    file_paths: tuple[str, ...]

    def matches(
        self, course_id: str, assignment_id: str, file_paths: Iterable[Path]
    ) -> bool:
        return (
            self.course_id == str(course_id)
            and self.assignment_id == str(assignment_id)
            and self.file_paths == _normalized_paths(file_paths)
        )


def create_approval(
    *,
    confirmed: bool,
    course_id: str,
    assignment_id: str,
    file_paths: Iterable[Path],
) -> ApprovalSnapshot:
    """Create an immutable approval only when all submission inputs are present."""
    paths = _normalized_paths(file_paths)
    if not confirmed:
        raise ApprovalRequiredError("Confirm the reviewed batch before submitting.")
    if not course_id or not assignment_id:
        raise ApprovalRequiredError("Select a course and assignment first.")
    if not paths:
        raise ApprovalRequiredError("Include at least one file in the batch.")
    return ApprovalSnapshot(str(course_id), str(assignment_id), paths)


def require_matching_approval(
    approval: ApprovalSnapshot | None,
    *,
    course_id: str,
    assignment_id: str,
    file_paths: Iterable[Path],
) -> None:
    """Block submission if anything changed after the user's confirmation."""
    if approval is None or not approval.matches(course_id, assignment_id, file_paths):
        raise ApprovalRequiredError(
            "The batch changed after confirmation. Review and confirm it again."
        )
