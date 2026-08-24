# Contributing

Thanks for helping improve Canvas Batch Uploader.

## Local setup

Use Python 3.11 or newer, create a virtual environment, and install the dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Checks before a pull request

Run the offline test suite and syntax check:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app tests test_canvas_api.py
```

Tests must not call a real Canvas instance or require a Canvas token. Do not commit `.env` files, tokens, course data, downloaded files, exports, or other private material.

Please describe behavioral changes clearly, especially changes involving uploads, submissions, redirects, credentials, or local-file access. Preserve the explicit review and confirmation safeguards when changing write workflows.
