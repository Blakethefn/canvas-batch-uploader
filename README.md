# Canvas Batch Uploader

A small Tkinter desktop app for reviewing and submitting a folder of local files to one Canvas assignment. It never selects files automatically and never creates a submission without an explicit confirmation.

## Setup

Use Python 3.11 or newer. From this project directory in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Open the new `.env` file locally and replace the placeholders:

```dotenv
API_KEY=your_canvas_personal_access_token
CANVAS_BASE_URL=https://your-school.instructure.com
```

Canvas personal access tokens are normally created under **Account → Settings → Approved Integrations → New Access Token**. Your school may restrict token creation; follow its Canvas guidance if that option is unavailable. Use only the HTTPS origin for your school, without `/api/v1` or a trailing page path.

Never share `.env` or paste its token into an issue, log, or screenshot. The real `.env`, `data/`, `exports/`, and `PROJECT_GOAL.md` are Git-ignored.

## Run

With the virtual environment active:

```powershell
python -m app
```

The app loads active courses, then assignments for the selected course. Choose a local folder, explicitly include the desired files, select a file-upload assignment, and inspect the review table. The Submit button remains disabled until you check the confirmation box for that exact batch.

### Homework Library

The desktop app includes a **Homework Library** section for keeping local work organized:

1. Select a Canvas course and assignment.
2. Choose a private homework folder for that course, such as `D:\accountings2`.
3. Click **Add homework files** and choose completed files.
4. The app copies them into an assignment-named subfolder and loads the stored copies into the review table.

Original files are never moved or deleted. Existing files are never overwritten: identical files are reused, while a different file with the same name receives a numbered name such as `worksheet (2).xlsx`. Credential files, private keys, and symbolic links are rejected. Course-to-folder mappings are stored only in the Git-ignored `data/homework_library.json`; homework file contents remain in the folder you selected.

## Headless MCP server

The MCP server uses stdio and does not import or start Tkinter. Its default mode can inspect Canvas, list explicitly named local folders, and prepare reviews, but cannot upload or submit:

```powershell
python -m app.mcp_server
```

To start a write-capable process, the operator must explicitly add the flag below. Preparing and submitting must happen through the same running process because approval IDs exist only in memory:

```powershell
python -m app.mcp_server --enable-submit
```

Even in write-capable mode, a submission requires a non-expired prepared batch and the exact returned confirmation `SUBMIT <batch-id>`. Approvals expire after 10 minutes, are erased at shutdown, and are consumed after a successful submission. Do not run submission-enabled mode merely to inspect Canvas.

### Register with Codex

The current [official OpenAI MCP documentation](https://developers.openai.com/codex/mcp) uses `codex mcp add <name> -- <stdio command>`. From Windows PowerShell, register the safe default server with the project virtual environment:

```powershell
codex.cmd mcp add canvas-batch-uploader --env PYTHONPATH=D:\canvas-batch-uploader -- D:\canvas-batch-uploader\.venv\Scripts\python.exe -m app.mcp_server
codex.cmd mcp list
```

The `PYTHONPATH` value only makes the local `app` package importable when Codex starts it; it is not a credential. The server reads `API_KEY` and `CANVAS_BASE_URL` from the existing private `D:\canvas-batch-uploader\.env`. Never put the Canvas token in the registration command or Codex configuration.

On macOS or Linux, use the equivalent project and virtual-environment paths:

```sh
codex mcp add canvas-batch-uploader --env PYTHONPATH=/absolute/path/to/canvas-batch-uploader -- /absolute/path/to/canvas-batch-uploader/.venv/bin/python -m app.mcp_server
```

Codex also supports a `cwd` field for stdio servers in `~/.codex/config.toml`; the CLI example above uses `PYTHONPATH` so it works without a manual config edit.

### MCP tools

- `canvas_configuration_status`: reports presence and URL validity without returning any token.
- `canvas_list_active_courses` and `canvas_list_assignments`: read Canvas course and assignment metadata.
- `canvas_list_local_files`: lists only safe, direct regular files in one absolute folder and never reads their contents.
- `canvas_prepare_batch`: validates the target and exact file contents, verifies no existing submission, and returns a complete expiring review without uploading.
- `canvas_submit_prepared_batch`: write-enabled only; revalidates the immutable review, uploads every file, then creates exactly one submission.
- `canvas_retry_failed_uploads`: write-enabled only; retries only failed uploads while retaining successful in-memory receipts. It is permanently blocked after an uncertain final submission request.

## Safety behavior

- The token is read from `.env`, used only in the Canvas `Authorization` header, and never displayed or logged.
- Files named `.env` or `.env.*` are excluded from folder selections so credentials cannot be added to a batch accidentally.
- Canvas API redirects are blocked. Pagination links and upload-completion URLs are accepted only when they return to the configured Canvas origin. A Canvas-provided HTTPS upload service receives the file but never receives the Canvas authorization header.
- The app checks for an existing submission before uploading and again immediately before creating the submission. It refuses to replace or add to an existing submission.
- All files in an approved batch upload before one submission is created. If a file upload fails, no submission is created; use **Retry failed uploads** after fixing the local problem.
- If the final submission response is uncertain, the app disables retries and asks you to verify in Canvas, avoiding an accidental resubmission.
- Only small timestamped JSON result summaries are saved under `exports/`. They contain file names, Canvas course/assignment IDs, and outcomes—not tokens or file contents.
- Network work runs only after visible actions. There are no automatic or hidden uploads.
- The MCP server is read-only by default. Its submission tools are additionally marked as destructive, non-idempotent MCP writes so compatible clients can apply their strongest write approval policy.
- MCP approval snapshots and upload receipts remain in memory only. Local files are fingerprinted at preparation and rechecked immediately before upload; a changed file or assignment invalidates the approval.

## Tests

Tests are offline and do not call Canvas:

```powershell
python -m unittest discover -s tests -v
```

Syntax-check all application and test modules with:

```powershell
python -m compileall -q app tests test_canvas_api.py
```

The existing `test_canvas_api.py` remains a separate read-only proof script; it is not needed to run the desktop app.
