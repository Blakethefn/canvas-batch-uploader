# Canvas Batch Uploader

Canvas Batch Uploader is a Tkinter desktop app, with an included headless MCP server, that uses Canvas's official REST API to automate common course, assignment, file-upload, and submission workflows. It is a productivity tool for reducing repetitive manual interaction with Canvas: users can review and submit files through the GUI, while MCP-compatible AI agents can inspect courses and assignments, retrieve assignment files, prepare an explicit batch review, and—when separately enabled and approved—upload files and create a submission.

Both interfaces operate with the user's own authorized Canvas credentials and respect the permissions granted to those credentials. The project does not bypass Canvas permissions, deadlines, or access controls, and it does not invent or infer authorization to act. The MCP process is read-only by default; write actions require explicit opt-in and confirmation.

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

For vulnerability reports or credential-exposure concerns, see [SECURITY.md](SECURITY.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for local development and test instructions.

## Desktop app

With the virtual environment active:

```powershell
python -m app
```

The app loads active courses, then assignments for the selected course. Choose a local folder, explicitly include the desired files, select a file-upload assignment, and inspect the review table. The Submit button remains disabled until you check the confirmation box for that exact batch.

### Homework Library

The desktop app includes a **Homework Library** section for keeping local work organized:

1. Select a Canvas course and assignment.
2. Choose a private homework folder for that course, such as `D:\accountings2`.
3. Click **Download Canvas files** to save Canvas-hosted attachments and files linked in the assignment instructions. Existing local files are never overwritten.
4. Click **Add homework files** and choose completed files.
5. The app copies them into an assignment-named subfolder and loads the stored copies into the review table.

Downloaded assignment files are loaded into the file table but are not automatically selected for upload. Original files are never moved or deleted. Existing files are never overwritten: downloaded conflicts receive a numbered name, while locally added identical files are reused and different files receive names such as `worksheet (2).xlsx`. Credential files, private keys, and symbolic links are rejected from upload storage. Course-to-folder mappings are stored only in the Git-ignored `data/homework_library.json`; homework file contents remain in the folder you selected.

## MCP server

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
- `canvas_list_assignment_files`: lists Canvas-hosted attachments and file links for one assignment without downloading.
- `canvas_download_assignment_files`: downloads those files into an explicitly named existing absolute folder without overwriting local files. This local-only write does not require `--enable-submit`.
- `canvas_list_local_files`: lists only safe, direct regular files in one absolute folder and never reads their contents.
- `canvas_prepare_batch`: validates the target and exact file contents, verifies no existing submission, and returns a complete expiring review without uploading.
- `canvas_submit_prepared_batch`: write-enabled only; revalidates the immutable review, uploads every file, then creates exactly one submission.
- `canvas_retry_failed_uploads`: write-enabled only; retries only failed uploads while retaining successful in-memory receipts. It is permanently blocked after an uncertain final submission request.

## Safety behavior

- The token is read from `.env`, used only in the Canvas `Authorization` header, and never displayed or logged.
- Files named `.env` or `.env.*` are excluded from folder selections so credentials cannot be added to a batch accidentally.
- Canvas API redirects are blocked. Pagination links and upload-completion URLs are accepted only when they return to the configured Canvas origin. A Canvas-provided HTTPS upload service receives the file but never receives the Canvas authorization header.
- Assignment downloads follow only HTTPS links. The Canvas token is sent only to the configured Canvas origin and is removed before any download redirect to another host.
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

## Project status

This project is provided as source code for local use. Canvas access requires a Canvas instance, an authorized personal access token, and whatever permissions and submission policies that institution applies.
