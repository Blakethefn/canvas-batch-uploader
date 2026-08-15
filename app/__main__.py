"""Launch the Canvas Batch Uploader desktop app."""

from __future__ import annotations

from pathlib import Path
from tkinter import Tk, messagebox

from .canvas_client import CanvasClient
from .config import AppConfig, ConfigurationError
from .ui import CanvasUploaderApp


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    root = Tk()
    root.withdraw()
    try:
        config = AppConfig.from_project(project_root)
    except ConfigurationError as error:
        messagebox.showerror("Configuration error", str(error), parent=root)
        root.destroy()
        return 2

    root.title("Canvas Batch Uploader")
    root.minsize(1000, 820)
    CanvasUploaderApp(
        root,
        CanvasClient(config.canvas_base_url, config.api_key),
        project_root,
    )
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
