from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.homework_library import (
    HomeworkLibrary,
    HomeworkLibraryError,
    safe_folder_name,
)


class HomeworkLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.homework_root = self.root / "accounting"
        self.homework_root.mkdir()
        self.settings = self.root / "data" / "homework_library.json"
        self.library = HomeworkLibrary(self.settings)
        self.library.save_course_folder("100", "Accounting", self.homework_root)

    def test_course_folder_mapping_is_saved_and_reloaded(self) -> None:
        reloaded = HomeworkLibrary(self.settings)
        self.assertEqual(reloaded.course_folder("100"), self.homework_root.resolve())
        self.assertNotIn("API_KEY", self.settings.read_text(encoding="utf-8"))

    def test_files_are_copied_into_safe_assignment_folder(self) -> None:
        source = self.root / "chapter-1.pdf"
        source.write_bytes(b"answer")
        folder, records = self.library.store_files(
            "100", "Week 1: Journal / Entries", [source]
        )
        self.assertEqual(folder.name, "Week 1- Journal - Entries")
        self.assertEqual(records[0].destination.read_bytes(), b"answer")
        self.assertEqual(source.read_bytes(), b"answer")

    def test_identical_file_is_not_duplicated_and_changed_file_is_renamed(self) -> None:
        source = self.root / "worksheet.xlsx"
        source.write_bytes(b"first")
        _, first = self.library.store_files("100", "Unit 1", [source])
        _, duplicate = self.library.store_files("100", "Unit 1", [source])
        self.assertTrue(first[0].copied)
        self.assertFalse(duplicate[0].copied)

        source.write_bytes(b"updated")
        _, changed = self.library.store_files("100", "Unit 1", [source])
        self.assertEqual(changed[0].destination.name, "worksheet (2).xlsx")
        self.assertEqual(changed[0].destination.read_bytes(), b"updated")
        _, repeated_change = self.library.store_files("100", "Unit 1", [source])
        self.assertFalse(repeated_change[0].copied)
        self.assertEqual(repeated_change[0].destination.name, "worksheet (2).xlsx")

    def test_sensitive_file_blocks_batch_before_any_copy(self) -> None:
        safe = self.root / "answer.txt"
        sensitive = self.root / ".env"
        safe.write_text("answer", encoding="utf-8")
        sensitive.write_text("API_KEY=placeholder", encoding="utf-8")
        with self.assertRaises(HomeworkLibraryError):
            self.library.store_files("100", "Unit 2", [safe, sensitive])
        self.assertFalse((self.homework_root / "Unit 2").exists())

    def test_folder_name_cannot_escape_homework_root(self) -> None:
        self.assertEqual(safe_folder_name("../../Final: Exam"), "-..-Final- Exam")
        destination = self.library.assignment_folder("100", "../../Final: Exam")
        self.assertEqual(destination.parent, self.homework_root.resolve())


if __name__ == "__main__":
    unittest.main()
