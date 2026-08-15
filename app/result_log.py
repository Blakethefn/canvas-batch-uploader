"""Small privacy-conscious local result logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


def write_results_log(
    output_directory: Path,
    *,
    course_id: str,
    assignment_id: str,
    results: Iterable[Mapping[str, object]],
) -> Path:
    """Write operational results without credentials or uploaded file contents."""
    output_directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    destination = output_directory / f"submission_results_{now:%Y%m%dT%H%M%S%fZ}.json"
    payload = {
        "completed_at_utc": now.isoformat(),
        "course_id": str(course_id),
        "assignment_id": str(assignment_id),
        "results": list(results),
    }
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return destination
