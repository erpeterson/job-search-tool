# Job Search Tool

Local CRM and scoring tool for Eric's job search.

Run:

```bash
job-search-tool/run.sh
```

The script will:

- create `job-search-tool/.venv` if needed
- install dependencies from `job-search-tool/requirements.txt` if needed
- create `job-search-tool/.env` if needed
- configure `CODEX_CLI_PATH=codex` when the Codex CLI is on `PATH`
- start the local Flask app

To prepare the environment without starting the app:

```bash
job-search-tool/run.sh --setup-only
```

Then open:

```text
http://127.0.0.1:5050
```

Command-line options (also accepted by `python app.py` or `python -m job_search`):

| Flag | Purpose |
| --- | --- |
| `-v`, `--verbose` | Write non-error logs to stdout. ERROR logs always go to stderr. |
| `--host HOST` | Bind address; overrides `JOB_SEARCH_HOST`. |
| `--port PORT` | Port; overrides `JOB_SEARCH_PORT`. |

Exit codes: `0` success, `1` unexpected failure, `2` invalid configuration,
`130` interrupted.

## Architecture

The app is a `job_search` package split into three tiers:

- `job_search/web/` and `job_search/cli.py` (presentation): Flask routes,
  request validation, the static UI, and the CLI entry point.
- `job_search/domain/` (business logic): search runs and discovery filters,
  scoring, application packets, level calibration, job CRM, and background tasks.
- `job_search/data/` (data access): SQLite repositories behind a unit of work,
  the Codex CLI adapter, job-board adapters, HTTP capture/replay, and file stores.

`job_search/container.py` is the composition root that wires them together.
`app.py` is a thin entry point kept for `run.sh`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # runs tests with coverage; fails below 80%
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run without network access, Codex, or Pandoc; those are replaced by fakes
in `tests/conftest.py`.

## Configuration

Values are read from the environment and `job-search-tool/.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `CODEX_CLI_PATH` | `codex` on `PATH` | Editable in the UI, but only as `codex` (found on `PATH`) or an absolute path to an executable named `codex`, because the value is run as a subprocess. Other values are rejected with `400`. |
| `CODEX_MODEL` | blank (Codex default) | Editable in the UI. |
| `CODEX_CLI_TIMEOUT_SECONDS` | `270` | Per Codex invocation. |
| `JOB_SEARCH_ENABLE_GPT_SCORING` | `0` | `1` enables Codex scoring. Editable in the UI. |
| `JOB_SEARCH_USE_CAPTURE_CACHE` | `1` | `0` forces live requests. Editable in the UI. |
| `JOB_SEARCH_HOST` / `JOB_SEARCH_PORT` | `127.0.0.1` / `5050` | Bind address. |
| `JOB_SEARCH_ALLOWED_HOSTS` | `127.0.0.1:<port>,localhost:<port>` | Comma-separated `Host` header values the app accepts. Other hosts get `403` (DNS-rebinding protection); state-changing requests with a foreign `Origin` also get `403`. |
| `JOB_SEARCH_INTERVAL_SECONDS` | `86400` | Scheduled search cadence (scheduler currently disabled). |
| `JOB_SEARCH_LOG_MAX_BYTES` / `JOB_SEARCH_LOG_BACKUP_COUNT` | `1048576` / `5` | Log rotation. |
| `JOB_SEARCH_WORKSPACE_ROOT` | parent of `job-search-tool/` | Location of `career-manual/`, `resume/`, `applications/`. |
| `JOB_SEARCH_ALLOW_REMOTE` | `0` | Set `1` to allow a non-loopback `JOB_SEARCH_HOST`. The API has no authentication, so only do this on a trusted network. |
| `JOB_SEARCH_MAX_REQUEST_BYTES` | `1048576` | Largest accepted request body; larger requests get `413`. |
| `JOB_SEARCH_HTTP_MAX_RESPONSE_BYTES` | `5242880` | Largest job-board or posting response the app reads; larger responses fail with `http_response_too_large`. |
| `JOB_SEARCH_DEBUG` | `0` | Flask debug mode. Refused with a non-loopback host because the debugger allows remote code execution. |

Invalid values stop startup with exit code `2` and a message naming the variable.

The app is served by the Werkzeug development server and is meant for local,
single-user use only. It refuses to bind to a non-loopback address unless
`JOB_SEARCH_ALLOW_REMOTE=1` is set.

## Errors, Logs, And Troubleshooting

- State-changing requests must send JSON (`Content-Type: application/json`) or
  no body at all; malformed JSON returns `400`.
- API errors return JSON `{"error": ..., "request_id": ...}` with `400`
  (validation), `403` (disallowed host or cross-origin request), `404`, `409` (conflict or Codex/scoring unavailable), `413` (body too large),
  `415` (non-empty body that is not `application/json`), `502`
  (Codex/Pandoc failure), or `500`. A `500` never includes internal details;
  search the logs for its `request_id`.
- Every response has an `X-Request-ID` header. Search runs and bulk tasks log a
  `correlation_id` of `search-run-<id>` or `task-<id>`.
- Each caught exception writes an `exception` event with a stable `error_code`
  and a `blame_metric` event to `logs/job-search.log`. `GET /api/metrics`
  returns the in-process blame counters.
- `Codex CLI is unavailable`: set `CODEX_CLI_PATH` in the Configuration panel.
- `Pandoc is required`: install Pandoc (`brew install pandoc`).
- Job-board `403`s: see `logs/api.log`; retry later or add the job by URL.
- Outbound fetches (job boards and posting URLs) only go to public addresses:
  every hostname is resolved and each redirect hop (at most 5) is re-checked.
  A posting URL that resolves or redirects to a private, loopback, or reserved
  address fails with `http_host_resolves_private` or `http_redirect_blocked`.

Codex-backed features require a locally installed and authenticated Codex CLI.
This includes application packet generation and optional Codex scoring.
If `codex` is not on `PATH`, pass its executable path:

```bash
job-search-tool/run.sh --codex-cli /path/to/codex
```

This writes `CODEX_CLI_PATH` to `job-search-tool/.env`.
Leave `CODEX_MODEL` blank unless you need to force a specific Codex-supported
model; blank uses your Codex CLI default.

Data is stored locally in `job-search-tool/job_search.sqlite3`.

Codex CLI path and model can also be updated from the app's Configuration panel.
Saved values are written to `job-search-tool/.env`, applied to the running
process, and displayed only in masked form.

Codex invocations default to a 270-second timeout. Override this when needed by
setting `CODEX_CLI_TIMEOUT_SECONDS` in `job-search-tool/.env`.

Application packet generation uses a read-only, JSON-only Codex drafting call.
The app provides the captured job posting, the relevant Career Manual rules, and
the master resume as a compact context, then validates and writes `Job-Brief.md`,
`Resume.md`, and `Cover-Letter.md` itself. Pandoc generates the matching DOCX
files locally. This removes repository exploration and filesystem/document work
from the Codex invocation. The Codex drafting call follows the repository's
`AGENTS.md` guidance, including the required AI-generation attribution in the
Markdown artifacts; Pandoc carries it into the generated DOCX files. Set
`CODEX_MODEL` only to override the Codex CLI default. The app introspects the
actual invocation and retries packet drafting with verified model metadata when
needed to ensure accurate attribution.

Rotating structured API logs are written as newline-delimited JSON to:

```text
job-search-tool/logs/api.log
```

Every actual Codex CLI subprocess emits `codex_cli_call_started` and
`codex_cli_call_completed` entries in the decision log. The completion entry
records the outcome, exit code, and total elapsed time in milliseconds and
seconds. Replay-cache hits are logged separately because they do not invoke
Codex.

The logs capture job-board request URL, method, status code, elapsed time, error
type, and a short response excerpt. They are intended for troubleshooting board
blocks such as Indeed `403` responses.

Filtering, discovery, duplicate-skip, capture, replay, and Codex-disabled
decisions are written as structured newline-delimited JSON to:

```text
job-search-tool/logs/job-search.log
```

Replayable request/response captures are stored under:

```text
job-search-tool/captures/
```

Captured job-board responses are keyed by request payload, so rerunning the same
search can replay the saved response instead of repeatedly hitting LinkedIn or
Indeed. Codex CLI request/response payloads use the same capture mechanism for
scoring and search refinement when Codex scoring is enabled.

Manual searches include a `Force refresh` checkbox. When checked, the search
bypasses replay and makes live LinkedIn, Indeed, and Codex CLI requests, then
writes the fresh responses back to captures. Scheduled searches always force
refresh so daily automation checks the boards instead of replaying old responses.

Codex scoring is disabled by default. To re-enable it, set this in the app's
Configuration panel or in `job-search-tool/.env`:

```text
JOB_SEARCH_ENABLE_GPT_SCORING=1
```

When Codex scoring is enabled and the CLI is available, each manually added job
is scored immediately after it is saved. If scoring cannot run, the job is still
saved and the app displays the reason.

Response replay is enabled by default. To force live requests, set:

```text
JOB_SEARCH_USE_CAPTURE_CACHE=0
```


## Automated Search

The app can run saved LinkedIn and Indeed searches:

- automatically on a daily cadence (currently disabled; see below)
- manually from the Search panel

The default queries are derived from `supporting-documents/20260731-job-search-guidance.md`.

There are at least eight seeded searches:

- Executive IC on LinkedIn
- Executive IC on Indeed
- Office of the CTO on LinkedIn
- Office of the CTO on Indeed
- Adjacent industries on LinkedIn
- Adjacent industries on Indeed
- Wildcards on LinkedIn
- Wildcards on Indeed

Those searches cover the four pipelines:

- Executive IC
- Office of the CTO
- Adjacent industries
- Wildcards

Scheduled daily searches are currently disabled in code (`SCHEDULER_SUPPORTED` in
`job_search/config.py`) because the scheduler has known bugs and consumes Codex
credits unattended. `JOB_SEARCH_AUTORUN` is ignored until it is re-enabled.

Set `JOB_SEARCH_INTERVAL_SECONDS` to change the cadence.

Each time a saved search runs, the app uses the scored discoveries from that pipeline and board to refine the saved keywords and criteria. The refinement loop is intentionally based on the pipeline descriptions and score outcomes in this tool, not on your LinkedIn or Indeed profile searches.

Important limitation:

- LinkedIn and Indeed public docs primarily expose partner/employer/ATS APIs, not open candidate job-search APIs.
- This tool uses best-effort public search adapters for LinkedIn and Indeed and records source errors in search runs when a board blocks, rate-limits, or changes markup.
- If proper partner APIs or callback/webhook access becomes available, the app is structured so those can replace the current adapters.

## Location Filtering

Search queries may stay broad, but discovered results pass through a separate
global location filter before tracking.

Included results:

- US-based remote roles.
- Seattle-based or Seattle-area roles.

Excluded results are written to `discovered_jobs` as rejected discoveries and to
the structured decision log with the location rejection reason.

## Compensation Filtering

Discovered results with explicit compensation below `$200,000/year` are rejected
before tracking. Missing compensation is not rejected because many postings omit
pay. Hourly and monthly amounts are annualized before evaluation.

## Sales Role Filtering

Search criteria explicitly exclude Account Executive and other sales roles.
Discovered results with sales-role titles are rejected before tracking.

## Level Filtering

The app treats Oracle Software Engineer `IC-6` as `Architect` and uses that as
the target level for job filtering. Runtime Levels.fyi API responses are not used
because the available endpoint returns paywall text instead of usable comparison
data.

Company/title calibrations are cached locally. When no cached calibration exists,
the app applies a conservative title taxonomy: obvious below-IC6 titles are
marked downlevel, obvious IC6-plus titles are marked in-range, and ambiguous
titles stay `Unknown - level not assessed`.

Discovery rule:

- If a discovered job appears downlevel from Oracle IC6-equivalent, it is tracked but hidden from the default jobs view.
- Rejected discoveries remain visible in the discovery log with the rejection reason.
- Jobs below the normal Codex threshold are tracked but filtered from the default job list only when Codex scoring is enabled.
- When Codex scoring is disabled, low or missing Codex scores do not filter jobs.
- Cached downlevel equivalencies mark matching jobs as downlevel before Codex scoring. The cache starts empty; rows are added only from local title calibration or future explicit calibration mechanisms.
- When level cannot be estimated, the level status is shown as `Unknown - level not assessed`.

## Product Rules

- The rubric is based on `supporting-documents/20260731-job-search-guidance.md`.
- Codex score threshold defaults to `40`.
- User score threshold defaults to `60`.
- A job is filtered when it is downlevel, when user score is below threshold, or when Codex scoring is enabled and Codex score is below threshold.
- Codex scoring includes recent user-scored examples as calibration context, so the model can adapt to Eric's preferences over time.
- Thresholds are editable in the UI and persisted in SQLite.
- The tracked jobs table includes fine-grained table filters for pipeline, source, filtered/downlevel visibility, text search, and included statuses. By default it hides filtered/downlevel rows plus terminal `rejected` and `declined` statuses.
- Company interest is tracked independently from individual roles. Company records can store interest status, interest score, rationale, contacts, notes, and next step while still showing matching tracked jobs for context.
- The Configuration panel includes an advanced `Purge tracked jobs` command for user-acceptance testing. It requires typing `PURGE`, deletes tracked jobs and their CRM notes/interactions, and preserves searches, settings, logs, captures, and discovery history.

## Current Scope

- Add and track job listings from a posting URL and pipeline.
- Scrape company, title, location, and posting text from manually entered URLs.
- Generate application packets by invoking Codex CLI with a repository-guided packet prompt, so UI-generated packets follow the same Career Manual and `AGENTS.md` guidance as direct Codex-generated packets.
- Generate Codex scorecards when Codex scoring is enabled and the Codex CLI is available.
- Select multiple tracked jobs and start async bulk Codex scorecard population or bulk application packet generation, then poll progress from the UI while the server processes jobs in the background.
- Record Eric's own scorecard.
- Track application status.
- Track company-level interest separately from individual roles.
- Track people, conversations, notes, and next steps.
- Filter low-fit jobs.
- Run saved LinkedIn and Indeed searches on demand or daily, with the next scheduled run shown in the UI.
- Hide downlevel discoveries by default while retaining them in tracked jobs.
