"""Private local organization for course homework folders."""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .batch_service import is_sensitive_local_file


class HomeworkLibraryError(ValueError):
    """Raised when homework cannot be stored safely."""


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class StoredHomework:
    source: Path
    destination: Path
    copied: bool


def safe_folder_name(value: str, *, fallback: str = "Homework") -> str:
    """Convert a Canvas label to one safe, readable local folder component."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned} homework"
    return cleaned[:100].rstrip(" .") or fallback


class HomeworkLibrary:
    """Remember course roots and copy files into assignment subfolders."""

    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path
        self._course_folders: dict[str, dict[str, str]] = {}
        self.load_error: str | None = None
        self._load()

    def course_folder(self, course_id: str) -> Path | None:
        record = self._course_folders.get(str(course_id))
        if not record:
            return None
        return Path(record["path"])

    def save_course_folder(
        self, course_id: str, course_name: str, folder: Path
    ) -> Path:
        if not str(course_id):
            raise HomeworkLibraryError("A Canvas course must be selected.")
        try:
            resolved = folder.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HomeworkLibraryError(
                "The homework folder does not exist or is inaccessible."
            ) from error
        if not resolved.is_dir():
            raise HomeworkLibraryError("The homework location must be a folder.")
        self._course_folders[str(course_id)] = {
            "course_name": str(course_name),
            "path": str(resolved),
        }
        self._save()
        self.load_error = None
        return resolved

    def assignment_folder(
        self,
        course_id: str,
        assignment_name: str,
        *,
        create: bool = False,
    ) -> Path:
        root = self.course_folder(course_id)
        if root is None:
            raise HomeworkLibraryError(
                "Choose a homework folder for this course first."
            )
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HomeworkLibraryError(
                "The saved homework folder is missing or inaccessible."
            ) from error
        destination = root / safe_folder_name(assignment_name, fallback="Assignment")
        if create:
            try:
                destination.mkdir(parents=False, exist_ok=True)
            except OSError as error:
                raise HomeworkLibraryError(
                    "Could not create the assignment folder."
                ) from error
        return destination

    def store_files(
        self,
        course_id: str,
        assignment_name: str,
        source_paths: Iterable[Path],
    ) -> tuple[Path, list[StoredHomework]]:
        sources = list(source_paths)
        if not sources:
            raise HomeworkLibraryError("Choose at least one homework file.")

        validated_sources: list[Path] = []
        for source in sources:
            if is_sensitive_local_file(source):
                raise HomeworkLibraryError(
                    f"Credential and private-key files cannot be stored: {source.name}"
                )
            if source.is_symlink():
                raise HomeworkLibraryError(
                    f"Symbolic links cannot be stored: {source.name}"
                )
            try:
                source = source.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise HomeworkLibraryError(
                    f"The selected file is missing or inaccessible: {source.name}"
                ) from error
            if not source.is_file():
                raise HomeworkLibraryError(f"Not a regular file: {source.name}")
            validated_sources.append(source)

        destination_folder = self.assignment_folder(
            course_id, assignment_name, create=True
        )
        stored: list[StoredHomework] = []
        for source in validated_sources:
            destination, already_present = self._available_destination(
                destination_folder, source
            )
            if already_present:
                stored.append(StoredHomework(source, destination, copied=False))
                continue
            try:
                shutil.copy2(source, destination)
            except OSError as error:
                raise HomeworkLibraryError(
                    f"Could not copy {source.name} into the homework library."
                ) from error
            stored.append(StoredHomework(source, destination, copied=True))
        return destination_folder, stored

    def _load(self) -> None:
        if not self.settings_path.is_file():
            return
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            records = payload.get("course_folders", {})
            if not isinstance(records, dict):
                raise ValueError
            for course_id, record in records.items():
                if not isinstance(record, dict):
                    raise ValueError
                course_name = record.get("course_name")
                path = record.get("path")
                if not isinstance(course_name, str) or not isinstance(path, str):
                    raise ValueError
                self._course_folders[str(course_id)] = {
                    "course_name": course_name,
                    "path": path,
                }
        except (OSError, ValueError, json.JSONDecodeError):
            self._course_folders.clear()
            self.load_error = (
                "The saved homework-folder settings could not be read. "
                "Choosing a course folder will replace them."
            )

    def _save(self) -> None:
        payload = {"version": 1, "course_folders": self._course_folders}
        temporary_path = self.settings_path.with_suffix(".tmp")
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary_path, self.settings_path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise HomeworkLibraryError(
                "Could not save the private homework-folder settings."
            ) from error

    @staticmethod
    def _available_destination(folder: Path, source: Path) -> tuple[Path, bool]:
        def is_identical(candidate: Path) -> bool:
            try:
                return candidate.samefile(source) or (
                    candidate.is_file() and filecmp.cmp(candidate, source, shallow=False)
                )
            except OSError:
                return False

        stem, suffix = source.stem, source.suffix
        number = 1
        while True:
            candidate = (
                folder / source.name
                if number == 1
                else folder / f"{stem} ({number}){suffix}"
            )
            if not candidate.exists():
                return candidate, False
            if is_identical(candidate):
                return candidate, True
            number += 1
