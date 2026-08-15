"""Tkinter user interface for reviewing and submitting one safe file batch."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from typing import Callable, TypeVar

from .batch_service import BatchRunResult, is_sensitive_local_file, run_approved_batch
from .canvas_client import (
    CanvasAssignment,
    CanvasClient,
    CanvasCourse,
    CanvasError,
    UploadedFile,
)
from .result_log import write_results_log
from .homework_library import HomeworkLibrary, HomeworkLibraryError, safe_folder_name
from .submission_guard import (
    ApprovalRequiredError,
    ApprovalSnapshot,
    create_approval,
    require_matching_approval,
)


T = TypeVar("T")


class CanvasUploaderApp(ttk.Frame):
    """The complete lean desktop workflow."""

    def __init__(
        self, root: Tk, client: CanvasClient, project_root: Path
    ) -> None:
        super().__init__(root, padding=12)
        self.root = root
        self.client = client
        self.project_root = project_root
        self.courses: list[CanvasCourse] = []
        self.assignments: list[CanvasAssignment] = []
        self.files: list[Path] = []
        self.included: set[Path] = set()
        self.approval: ApprovalSnapshot | None = None
        self.uploaded: dict[Path, UploadedFile] = {}
        self.failed_paths: set[Path] = set()
        self._busy = False
        self.homework_library = HomeworkLibrary(
            project_root / "data" / "homework_library.json"
        )

        self.course_value = StringVar()
        self.assignment_value = StringVar()
        self.folder_value = StringVar(value="No folder selected")
        self.library_folder_value = StringVar(
            value="Select a Canvas course to choose its homework folder."
        )
        self.library_status_value = StringVar(
            value=self.homework_library.load_error or "Files are copied; originals stay in place."
        )
        self.confirmed = BooleanVar(value=False)
        self.status_value = StringVar(value="Loading active courses…")

        self._build_widgets()
        self.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(0, self._load_courses)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.rowconfigure(5, weight=1)

        target = ttk.LabelFrame(self, text="1. Choose the Canvas target", padding=8)
        target.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="Course").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.course_combo = ttk.Combobox(
            target, textvariable=self.course_value, state="readonly"
        )
        self.course_combo.grid(row=0, column=1, sticky="ew")
        self.course_combo.bind("<<ComboboxSelected>>", self._course_selected)
        self.reload_button = ttk.Button(
            target, text="Reload courses", command=self._load_courses
        )
        self.reload_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(target, text="Assignment").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.assignment_combo = ttk.Combobox(
            target, textvariable=self.assignment_value, state="readonly"
        )
        self.assignment_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.assignment_combo.bind("<<ComboboxSelected>>", self._assignment_selected)
        style = ttk.Style(self.root)
        style.configure("Completed.TLabel", foreground="#188038")
        self.assignment_status_value = StringVar(value="")
        self.assignment_status = ttk.Label(
            target,
            textvariable=self.assignment_status_value,
            style="Completed.TLabel",
        )
        self.assignment_status.grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(4, 0)
        )

        library = ttk.LabelFrame(
            self,
            text="2. Homework Library — organize files by course and assignment",
            padding=8,
        )
        library.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        library.columnconfigure(1, weight=1)
        ttk.Label(library, text="Course folder").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(library, textvariable=self.library_folder_value).grid(
            row=0, column=1, sticky="w"
        )
        self.library_folder_button = ttk.Button(
            library,
            text="Choose / change folder",
            command=self._choose_course_homework_folder,
        )
        self.library_folder_button.grid(row=0, column=2, padx=(8, 0))
        self.store_homework_button = ttk.Button(
            library,
            text="Add homework files",
            command=self._store_homework_files,
        )
        self.store_homework_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.load_homework_button = ttk.Button(
            library,
            text="Load stored assignment",
            command=self._load_stored_assignment,
        )
        self.load_homework_button.grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(library, textvariable=self.library_status_value).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        folder = ttk.Frame(self)
        folder.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        folder.columnconfigure(1, weight=1)
        self.folder_button = ttk.Button(
            folder, text="3. Choose any local folder", command=self._choose_folder
        )
        self.folder_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(folder, textvariable=self.folder_value).grid(row=0, column=1, sticky="w")

        files_frame = ttk.LabelFrame(
            self, text="4. Select files (nothing is uploaded yet)", padding=8
        )
        files_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)
        self.file_tree = ttk.Treeview(
            files_frame,
            columns=("included", "name", "size", "path"),
            show="headings",
            selectmode="extended",
            height=7,
        )
        self.file_tree.heading("included", text="Include")
        self.file_tree.heading("name", text="File")
        self.file_tree.heading("size", text="Size")
        self.file_tree.heading("path", text="Full local path")
        self.file_tree.column("included", width=65, stretch=False, anchor="center")
        self.file_tree.column("name", width=190)
        self.file_tree.column("size", width=85, stretch=False, anchor="e")
        self.file_tree.column("path", width=430)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        file_scroll = ttk.Scrollbar(
            files_frame, orient="vertical", command=self.file_tree.yview
        )
        file_scroll.grid(row=0, column=1, sticky="ns")
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        self.file_tree.bind("<Double-1>", self._toggle_double_clicked_file)

        file_actions = ttk.Frame(files_frame)
        file_actions.grid(row=1, column=0, sticky="w", pady=(7, 0))
        self.include_button = ttk.Button(
            file_actions, text="Include selected", command=lambda: self._set_selected_files(True)
        )
        self.include_button.grid(row=0, column=0)
        self.exclude_button = ttk.Button(
            file_actions, text="Exclude selected", command=lambda: self._set_selected_files(False)
        )
        self.exclude_button.grid(row=0, column=1, padx=(6, 0))
        self.include_all_button = ttk.Button(
            file_actions, text="Include all", command=self._include_all
        )
        self.include_all_button.grid(row=0, column=2, padx=(6, 0))

        review_label = ttk.Label(self, text="5. Review exactly what will be submitted")
        review_label.grid(row=4, column=0, sticky="w", pady=(0, 4))
        review_frame = ttk.Frame(self)
        review_frame.grid(row=5, column=0, sticky="nsew")
        review_frame.columnconfigure(0, weight=1)
        review_frame.rowconfigure(0, weight=1)
        self.review_tree = ttk.Treeview(
            review_frame,
            columns=("file", "course", "assignment", "path"),
            show="headings",
            height=6,
        )
        for column, title in (
            ("file", "File"),
            ("course", "Course"),
            ("assignment", "Assignment"),
            ("path", "Full local path"),
        ):
            self.review_tree.heading(column, text=title)
        self.review_tree.column("file", width=160)
        self.review_tree.column("course", width=160)
        self.review_tree.column("assignment", width=190)
        self.review_tree.column("path", width=390)
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        review_scroll = ttk.Scrollbar(
            review_frame, orient="vertical", command=self.review_tree.yview
        )
        review_scroll.grid(row=0, column=1, sticky="ns")
        self.review_tree.configure(yscrollcommand=review_scroll.set)

        approval_frame = ttk.Frame(self)
        approval_frame.grid(row=6, column=0, sticky="ew", pady=(8, 6))
        approval_frame.columnconfigure(0, weight=1)
        self.confirm_check = ttk.Checkbutton(
            approval_frame,
            text="I reviewed these exact files and approve submitting them to this assignment.",
            variable=self.confirmed,
            command=self._confirmation_changed,
        )
        self.confirm_check.grid(row=0, column=0, sticky="w")
        self.submit_button = ttk.Button(
            approval_frame, text="Submit approved batch", command=self._submit_batch, state="disabled"
        )
        self.submit_button.grid(row=0, column=1, padx=(8, 0))
        self.retry_button = ttk.Button(
            approval_frame, text="Retry failed uploads", command=self._retry_failed, state="disabled"
        )
        self.retry_button.grid(row=0, column=2, padx=(8, 0))

        progress_frame = ttk.Frame(self)
        progress_frame.grid(row=7, column=0, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.status_value).grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

        results_frame = ttk.LabelFrame(self, text="Per-file results", padding=8)
        results_frame.grid(row=8, column=0, sticky="ew")
        results_frame.columnconfigure(0, weight=1)
        self.results_tree = ttk.Treeview(
            results_frame,
            columns=("file", "result", "details"),
            show="headings",
            height=4,
        )
        self.results_tree.heading("file", text="File")
        self.results_tree.heading("result", text="Result")
        self.results_tree.heading("details", text="Details")
        self.results_tree.column("file", width=200)
        self.results_tree.column("result", width=115, stretch=False)
        self.results_tree.column("details", width=550)
        self.results_tree.grid(row=0, column=0, sticky="ew")

    def _load_courses(self) -> None:
        if self._busy:
            return
        self._invalidate_approval()
        self.course_value.set("")
        self.assignment_value.set("")
        self.assignment_status_value.set("")
        self.course_combo["values"] = ()
        self.assignment_combo["values"] = ()
        self._set_busy(True, "Loading active courses…")
        self._run_background(self.client.get_active_courses, self._courses_loaded)

    def _courses_loaded(self, courses: list[CanvasCourse]) -> None:
        self.courses = courses
        values = [self._course_label(course) for course in courses]
        self.course_combo["values"] = values
        self._set_busy(False, f"Loaded {len(courses)} active course(s).")
        if courses:
            self.course_combo.current(0)
            self._course_selected()

    def _course_selected(self, _event: object = None) -> None:
        if self._busy:
            return
        course = self._selected_course()
        if course is None:
            return
        self._invalidate_approval()
        self.assignments = []
        self.assignment_value.set("")
        self.assignment_status_value.set("")
        self.assignment_combo["values"] = ()
        self._refresh_library_status()
        self._refresh_review()
        self._set_busy(True, f"Loading assignments for {course.name}…")
        self._run_background(
            lambda: self.client.get_assignments(course.id), self._assignments_loaded
        )

    def _assignments_loaded(self, assignments: list[CanvasAssignment]) -> None:
        self.assignments = assignments
        self.assignment_combo["values"] = [
            self._assignment_label(assignment) for assignment in assignments
        ]
        self._set_busy(False, f"Loaded {len(assignments)} assignment(s).")
        if assignments:
            self.assignment_combo.current(0)
            self._assignment_selected()

    def _assignment_selected(self, _event: object = None) -> None:
        if self._busy:
            return
        self._invalidate_approval()
        assignment = self._selected_assignment()
        if assignment and assignment.is_submitted:
            self.assignment_status_value.set("✓ Already submitted in Canvas")
            self.status_value.set(
                "This assignment is already done. Choose another assignment to upload."
            )
        elif assignment and not assignment.accepts_file_uploads:
            self.assignment_status_value.set("")
            self.status_value.set("This assignment does not accept file uploads.")
        else:
            self.assignment_status_value.set("")
            self.status_value.set("Review the selected target and files.")
        self._refresh_library_status()
        self._refresh_review()

    def _choose_course_homework_folder(self) -> None:
        if self._busy:
            return
        course = self._selected_course()
        if course is None:
            messagebox.showwarning("Homework Library", "Select a Canvas course first.")
            return
        current = self.homework_library.course_folder(course.id)
        dialog_options: dict[str, object] = {
            "title": f"Choose the homework folder for {course.name}",
            "mustexist": True,
        }
        if current and current.is_dir():
            dialog_options["initialdir"] = str(current)
        chosen = filedialog.askdirectory(**dialog_options)
        if not chosen:
            return
        try:
            saved = self.homework_library.save_course_folder(
                course.id, course.name, Path(chosen)
            )
        except HomeworkLibraryError as error:
            messagebox.showerror("Homework Library", str(error))
            return
        self.library_folder_value.set(str(saved))
        self.library_status_value.set(
            f"Homework for {course.name} will be organized inside this folder."
        )

    def _store_homework_files(self) -> None:
        if self._busy:
            return
        course = self._selected_course()
        assignment = self._selected_assignment()
        if course is None or assignment is None:
            messagebox.showwarning(
                "Homework Library", "Select a Canvas course and assignment first."
            )
            return
        if self.homework_library.course_folder(course.id) is None:
            messagebox.showwarning(
                "Homework Library",
                "Choose a homework folder for this course first.",
            )
            return
        chosen = filedialog.askopenfilenames(
            title=f"Add files for {assignment.name}"
        )
        if not chosen:
            return
        try:
            folder, stored = self.homework_library.store_files(
                course.id,
                assignment.name,
                (Path(path) for path in chosen),
            )
        except HomeworkLibraryError as error:
            messagebox.showerror("Homework Library", str(error))
            return
        stored_paths = {record.destination for record in stored}
        self._load_folder(folder, included_paths=stored_paths)
        copied = sum(1 for record in stored if record.copied)
        already_present = len(stored) - copied
        detail = f"Stored {copied} new file(s) in {folder}."
        if already_present:
            detail += f" {already_present} identical file(s) were already there."
        self.library_status_value.set(detail)
        self.status_value.set(
            f"Loaded {len(stored_paths)} stored homework file(s) for review."
        )

    def _load_stored_assignment(self) -> None:
        if self._busy:
            return
        course = self._selected_course()
        assignment = self._selected_assignment()
        if course is None or assignment is None:
            messagebox.showwarning(
                "Homework Library", "Select a Canvas course and assignment first."
            )
            return
        try:
            folder = self.homework_library.assignment_folder(
                course.id, assignment.name
            )
        except HomeworkLibraryError as error:
            messagebox.showwarning("Homework Library", str(error))
            return
        if not folder.is_dir():
            messagebox.showinfo(
                "Homework Library",
                "No stored folder exists for this assignment yet. Use Add homework files first.",
            )
            return
        self._load_folder(folder)
        self.library_status_value.set(f"Loaded stored homework from {folder}.")

    def _choose_folder(self) -> None:
        if self._busy:
            return
        chosen = filedialog.askdirectory(title="Choose a folder of coursework files")
        if not chosen:
            return
        folder = Path(chosen).resolve(strict=False)
        self._load_folder(folder)

    def _load_folder(
        self, folder: Path, *, included_paths: set[Path] | None = None
    ) -> None:
        try:
            candidates = [path for path in folder.iterdir() if path.is_file()]
            self.files = sorted(
                (path for path in candidates if not is_sensitive_local_file(path)),
                key=lambda path: path.name.casefold(),
            )
        except OSError as error:
            messagebox.showerror("Folder error", f"Could not read that folder: {error}")
            return
        allowed_paths = set(self.files)
        self.included = (included_paths or set()) & allowed_paths
        self.folder_value.set(str(folder))
        self._invalidate_approval()
        self._refresh_file_tree()
        self._refresh_review()
        skipped = len(candidates) - len(self.files)
        skipped_note = f" Skipped {skipped} sensitive file(s)." if skipped else ""
        self.status_value.set(
            f"Found {len(self.files)} file(s). Select the files to include.{skipped_note}"
        )

    def _refresh_library_status(self) -> None:
        course = self._selected_course()
        if course is None:
            self.library_folder_value.set(
                "Select a Canvas course to choose its homework folder."
            )
            return
        folder = self.homework_library.course_folder(course.id)
        if folder is None:
            self.library_folder_value.set("No homework folder saved for this course.")
        else:
            self.library_folder_value.set(str(folder))
        assignment = self._selected_assignment()
        if assignment and folder:
            self.library_status_value.set(
                f"Files will be stored under: "
                f"{folder / safe_folder_name(assignment.name, fallback='Assignment')}"
            )

    def _set_selected_files(self, include: bool) -> None:
        if self._busy:
            return
        selected = [Path(item_id) for item_id in self.file_tree.selection()]
        if include:
            self.included.update(selected)
        else:
            self.included.difference_update(selected)
        self._batch_inputs_changed()

    def _include_all(self) -> None:
        if not self._busy:
            self.included = set(self.files)
            self._batch_inputs_changed()

    def _toggle_double_clicked_file(self, event: object) -> None:
        if self._busy:
            return
        item_id = self.file_tree.identify_row(getattr(event, "y", 0))
        if not item_id:
            return
        path = Path(item_id)
        if path in self.included:
            self.included.remove(path)
        else:
            self.included.add(path)
        self._batch_inputs_changed()

    def _batch_inputs_changed(self) -> None:
        self._invalidate_approval()
        self._refresh_file_tree()
        self._refresh_review()
        self.status_value.set(f"{len(self.included)} file(s) included. Review before confirming.")

    def _confirmation_changed(self) -> None:
        if not self.confirmed.get():
            self.approval = None
            self._update_action_buttons()
            return
        try:
            course, assignment, paths = self._validated_batch()
            self.approval = create_approval(
                confirmed=True,
                course_id=course.id,
                assignment_id=assignment.id,
                file_paths=paths,
            )
        except (ApprovalRequiredError, ValueError) as error:
            self.confirmed.set(False)
            self.approval = None
            messagebox.showwarning("Cannot confirm batch", str(error))
        self._update_action_buttons()

    def _submit_batch(self) -> None:
        if self._busy:
            return
        try:
            course, assignment, paths = self._validated_batch()
            require_matching_approval(
                self.approval,
                course_id=course.id,
                assignment_id=assignment.id,
                file_paths=paths,
            )
        except (ApprovalRequiredError, ValueError) as error:
            messagebox.showwarning("Submission blocked", str(error))
            return
        self.uploaded = {}
        self.failed_paths = set()
        self._clear_results()
        self._start_batch(course, assignment, paths, retry_only=False)

    def _retry_failed(self) -> None:
        if self._busy or not self.failed_paths:
            return
        try:
            course, assignment, paths = self._validated_batch()
            require_matching_approval(
                self.approval,
                course_id=course.id,
                assignment_id=assignment.id,
                file_paths=paths,
            )
        except (ApprovalRequiredError, ValueError) as error:
            messagebox.showwarning("Retry blocked", str(error))
            return
        self._start_batch(course, assignment, paths, retry_only=True)

    def _start_batch(
        self,
        course: CanvasCourse,
        assignment: CanvasAssignment,
        approved_paths: list[Path],
        *,
        retry_only: bool,
    ) -> None:
        approval = self.approval
        paths_to_upload = (
            [path for path in approved_paths if path in self.failed_paths]
            if retry_only
            else approved_paths
        )
        self.progress.configure(maximum=max(len(paths_to_upload) + 2, 1), value=0)
        self._set_busy(True, "Checking that the assignment has no existing submission…")

        def work() -> BatchRunResult:
            assert approval is not None
            return run_approved_batch(
                self.client,
                course,
                assignment,
                approved_paths,
                approval,
                uploaded=self.uploaded,
                retry_paths=paths_to_upload,
                progress=self._post_progress,
            )

        self._run_background(
            work,
            lambda result: self._batch_finished(
                course, assignment, approved_paths, result
            ),
            restore_busy=False,
        )

    def _batch_finished(
        self,
        course: CanvasCourse,
        assignment: CanvasAssignment,
        approved_paths: list[Path],
        result: BatchRunResult,
    ) -> None:
        self.uploaded = result.uploaded
        self.failed_paths = set(result.upload_errors)
        self._clear_results()
        log_rows: list[dict[str, str]] = []

        if result.submitted:
            self._mark_assignment_submitted(assignment.id)
            for path in approved_paths:
                self._add_result(path, "Success", "Submitted to Canvas.")
                log_rows.append(
                    {"file_name": path.name, "status": "success", "message": "Submitted."}
                )
            status = "Submission completed successfully."
        elif result.submission_error:
            for path in approved_paths:
                self._add_result(
                    path,
                    "Verify in Canvas",
                    "File uploaded, but submission status is uncertain. Do not retry here.",
                )
                log_rows.append(
                    {
                        "file_name": path.name,
                        "status": "verification_required",
                        "message": result.submission_error,
                    }
                )
            status = f"{result.submission_error} Check Canvas before taking another action."
        else:
            for path in approved_paths:
                if path in result.upload_errors:
                    detail = result.upload_errors[path]
                    self._add_result(path, "Failed", detail)
                    log_rows.append(
                        {"file_name": path.name, "status": "failed", "message": detail}
                    )
                else:
                    detail = "Uploaded and held; no submission was created because another file failed."
                    self._add_result(path, "Upload ready", detail)
                    log_rows.append(
                        {"file_name": path.name, "status": "upload_ready", "message": detail}
                    )
            if result.retry_safe:
                status = "No submission was created. Fix the failed files, then use Retry Failed."
            else:
                status = "No submission was created. Retrying is disabled for safety."

        try:
            log_path = write_results_log(
                self.project_root / "exports",
                course_id=course.id,
                assignment_id=assignment.id,
                results=log_rows,
            )
            status += f" Results saved to {log_path.relative_to(self.project_root)}."
        except OSError:
            status += " The local results log could not be written."

        self.progress.configure(value=self.progress["maximum"])
        self._set_busy(False, status)
        self.submit_button.configure(state="disabled")
        if result.submitted or result.submission_error or not result.retry_safe:
            self.confirmed.set(False)
            self.approval = None
            self._update_action_buttons()
        self.retry_button.configure(
            state=(
                "normal"
                if result.retry_safe
                and bool(self.failed_paths)
                and self.confirmed.get()
                else "disabled"
            )
        )

    def _validated_batch(
        self,
    ) -> tuple[CanvasCourse, CanvasAssignment, list[Path]]:
        course = self._selected_course()
        assignment = self._selected_assignment()
        paths = [path for path in self.files if path in self.included]
        if course is None:
            raise ValueError("Select a course.")
        if assignment is None:
            raise ValueError("Select an assignment.")
        if assignment.is_submitted:
            raise ValueError(
                "This assignment is already done in Canvas. Choose another assignment."
            )
        if not assignment.accepts_file_uploads:
            raise ValueError("The selected assignment does not accept file uploads.")
        if not paths:
            raise ValueError("Include at least one file.")
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            raise ValueError(f"These files are missing: {', '.join(missing)}")
        if assignment.allowed_extensions:
            disallowed = [
                path.name
                for path in paths
                if path.suffix.lstrip(".").lower() not in assignment.allowed_extensions
            ]
            if disallowed:
                allowed = ", ".join(assignment.allowed_extensions)
                raise ValueError(
                    f"Canvas allows only {allowed} for this assignment. Not allowed: "
                    + ", ".join(disallowed)
                )
        return course, assignment, paths

    def _selected_course(self) -> CanvasCourse | None:
        index = self.course_combo.current()
        return self.courses[index] if 0 <= index < len(self.courses) else None

    def _selected_assignment(self) -> CanvasAssignment | None:
        index = self.assignment_combo.current()
        return self.assignments[index] if 0 <= index < len(self.assignments) else None

    @staticmethod
    def _course_label(course: CanvasCourse) -> str:
        return f"{course.name} (ID {course.id})"

    @staticmethod
    def _assignment_label(assignment: CanvasAssignment) -> str:
        prefix = "✓ DONE — " if assignment.is_submitted else ""
        suffix = "" if assignment.accepts_file_uploads else " — no file uploads"
        return f"{prefix}{assignment.name} (ID {assignment.id}){suffix}"

    def _mark_assignment_submitted(self, assignment_id: str) -> None:
        index = next(
            (
                index
                for index, item in enumerate(self.assignments)
                if item.id == assignment_id
            ),
            None,
        )
        if index is None:
            return
        self.assignments[index] = replace(self.assignments[index], is_submitted=True)
        self.assignment_combo["values"] = [
            self._assignment_label(item) for item in self.assignments
        ]
        self.assignment_combo.current(index)
        self.assignment_status_value.set("✓ Already submitted in Canvas")

    def _refresh_file_tree(self) -> None:
        selections = set(self.file_tree.selection())
        self.file_tree.delete(*self.file_tree.get_children())
        for path in self.files:
            try:
                size = self._format_size(path.stat().st_size)
            except OSError:
                size = "Unavailable"
            item_id = str(path)
            self.file_tree.insert(
                "",
                "end",
                iid=item_id,
                values=("Yes" if path in self.included else "No", path.name, size, str(path)),
            )
            if item_id in selections:
                self.file_tree.selection_add(item_id)

    def _refresh_review(self) -> None:
        self.review_tree.delete(*self.review_tree.get_children())
        course = self._selected_course()
        assignment = self._selected_assignment()
        course_name = course.name if course else "Not selected"
        assignment_name = assignment.name if assignment else "Not selected"
        for path in self.files:
            if path in self.included:
                self.review_tree.insert(
                    "",
                    "end",
                    values=(path.name, course_name, assignment_name, str(path)),
                )

    def _invalidate_approval(self) -> None:
        self.confirmed.set(False)
        self.approval = None
        self.uploaded.clear()
        self.failed_paths.clear()
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        submit_state = "normal" if self.approval and not self._busy else "disabled"
        self.submit_button.configure(state=submit_state)
        if not self.failed_paths or not self.confirmed.get() or self._busy:
            self.retry_button.configure(state="disabled")

    def _set_busy(self, busy: bool, status: str) -> None:
        self._busy = busy
        self.status_value.set(status)
        standard_state = "disabled" if busy else "normal"
        combo_state = "disabled" if busy else "readonly"
        for button in (
            self.reload_button,
            self.folder_button,
            self.library_folder_button,
            self.store_homework_button,
            self.load_homework_button,
            self.include_button,
            self.exclude_button,
            self.include_all_button,
        ):
            button.configure(state=standard_state)
        self.course_combo.configure(state=combo_state)
        self.assignment_combo.configure(state=combo_state)
        self.confirm_check.configure(state=standard_state)
        self._update_action_buttons()

    def _run_background(
        self,
        work: Callable[[], T],
        on_success: Callable[[T], None],
        *,
        restore_busy: bool = True,
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except CanvasError as error:
                self.root.after(0, lambda: self._background_failed(str(error)))
            except Exception:
                self.root.after(
                    0,
                    lambda: self._background_failed("Unexpected application error."),
                )
            else:
                def finish() -> None:
                    if restore_busy and self._busy:
                        self._set_busy(False, "Ready.")
                    on_success(result)

                self.root.after(0, finish)

        threading.Thread(target=runner, daemon=True).start()

    def _background_failed(self, message: str) -> None:
        self._set_busy(False, message)
        messagebox.showerror("Canvas error", message)

    def _post_progress(self, value: int, status: str) -> None:
        self.root.after(
            0,
            lambda: (
                self.progress.configure(value=value),
                self.status_value.set(status),
            ),
        )

    def _clear_results(self) -> None:
        self.results_tree.delete(*self.results_tree.get_children())

    def _add_result(self, path: Path, result: str, detail: str) -> None:
        self.results_tree.insert("", "end", values=(path.name, result, detail))

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _on_close(self) -> None:
        if self._busy:
            messagebox.showwarning(
                "Operation in progress",
                "Wait for the current Canvas operation to finish before closing.",
            )
            return
        self.client.close()
        self.root.destroy()
