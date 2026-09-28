# Job Search Tool

Local CRM and scoring tool for Eric's job search.

Run:

```bash
job-search-tool/run.sh
```

The script requires Python 3.12+ (it exits with code `2` otherwise) and will:

- create `job-search-tool/.venv` if needed
- install pinned, hash-verified dependencies from `job-search-tool/requirements.lock` if needed
- create `job-search-tool/.env` if needed
- configure `CODEX_CLI_PATH=codex` when the Codex CLI is on `PATH`
- start the local Flask app

To prepare the environment without starting the app:

```bash
job-search-tool/run.sh --setup-only
```

Then open the address the app prints on startup (by default
`http://127.0.0.1:5050`).

Command-line options (also accepted by `python app.py` or `python -m job_search`):

| Flag | Purpose |
| --- | --- |
| `-v`, `--verbose` | Write non-error logs to stdout. ERROR logs always go to stderr. |
| `--host HOST` | Bind address; overrides `JOB_SEARCH_HOST`. |
| `--port PORT` | Port; overrides `JOB_SEARCH_PORT`. |

Subcommand:

```bash
.venv/bin/python app.py prune-captures --older-than 30        # list captures older than 30 days
.venv/bin/python app.py prune-captures --older-than 30 --yes  # archive them into captures/archive/
```

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
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pytest          # runs tests with coverage; fails below 80%
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pip-audit --require-hashes --disable-pip -r requirements.lock  # every pinned runtime package
```

Dependencies: `requirements.txt` and `requirements-dev.txt` list direct
dependencies; `requirements.lock` and `requirements-dev.lock` pin every
transitive package with hashes. After changing a direct dependency, regenerate
both locks:

```bash
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements.lock requirements.txt
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements-dev.lock requirements-dev.txt
```

The audit covers the full lock file, so transitive packages exposed to
untrusted input (Werkzeug, Jinja2, urllib3, soupsieve) are checked too. Last
run 2026-09-28 against `requirements.lock` and `requirements-dev.lock`: no known
vulnerabilities found, and no findings are accepted.

Ruff includes the `S` (bandit security) and `A` (builtin shadowing) rules; any
suppression is line-level and states its reason.

Tests run without network access, Codex, or Pandoc; those are replaced by fakes
in `tests/conftest.py`.

## Configuration

Values are read from the environment and `job-search-tool/.env`. `.env` values are
literal: `${VAR}` references are not expanded, so a saved value never pulls in
other environment variables.

| Variable | Default | Notes |
| --- | --- | --- |
| `CODEX_CLI_PATH` | `codex` on `PATH` | Editable in the UI, but only as `codex` (found on `PATH`) or an absolute path to an executable named `codex`, because the value is run as a subprocess. Other values are rejected with `400`. |
| `CODEX_MODEL` | blank (Codex default) | Editable in the UI. |
| `CODEX_CLI_TIMEOUT_SECONDS` | `270` | Per Codex invocation. |
| `PANDOC_TIMEOUT_SECONDS` | `120` | Per Pandoc DOCX conversion; timeouts fail with `pandoc_timeout`. |
| `JOB_SEARCH_ENABLE_GPT_SCORING` | `0` | `1` enables Codex scoring. Editable in the UI. |
| `JOB_SEARCH_USE_CAPTURE_CACHE` | `1` | `0` forces live requests. Editable in the UI. |
| `JOB_SEARCH_HOST` / `JOB_SEARCH_PORT` | `127.0.0.1` / `5050` | Bind address. |
| `JOB_SEARCH_ALLOWED_HOSTS` | `127.0.0.1:<port>,localhost:<port>` | Comma-separated `Host` header values the app accepts. Other hosts get `403` (DNS-rebinding protection); state-changing requests with a foreign `Origin` also get `403`. |
| `JOB_SEARCH_INTERVAL_SECONDS` | `86400` | Scheduled search cadence (scheduler currently disabled). |
| `JOB_SEARCH_LOG_MAX_BYTES` | `1048576` | Size at which a log is rotated into a timestamped `.gz` archive. |
| `JOB_SEARCH_WORKSPACE_ROOT` | parent of `job-search-tool/` | Location of `career-manual/`, `resume/`, `applications/`. |
| `JOB_SEARCH_PROFILE_PATH` | `<workspace>/job-search-profile.json`, else `profile.example.json` | Search profile JSON (see below). An explicit path must exist. |
| `JOB_SEARCH_DB_PATH` | `job-search-tool/job_search.sqlite3` | SQLite database file. Relative paths resolve against `job-search-tool/`. |
| `JOB_SEARCH_LOG_DIR` | `job-search-tool/logs` | Directory for `api.log` and `job-search.log`. |
| `JOB_SEARCH_CAPTURE_DIR` | `job-search-tool/captures` | Directory for replayable request/response captures. |
| `JOB_SEARCH_GUIDANCE_PATH` | `supporting-documents/20260731-job-search-guidance.md` | Search guidance file. Relative paths resolve against the workspace root. |
| `JOB_SEARCH_CAREER_MANUAL_PATH` | `career-manual/Career-Manual.md` | Career Manual file (workspace-relative). |
| `JOB_SEARCH_MASTER_RESUME_PATH` | `resume/Master-Resume.md` | Master resume file (workspace-relative). |
| `JOB_SEARCH_HTTP_TIMEOUT_SECONDS` | `30` | Timeout for each job-board or posting request. |
| `JOB_SEARCH_DB_TIMEOUT_SECONDS` | `30` | SQLite busy timeout. |
| `JOB_SEARCH_MAX_RETAINED_TASKS` | `50` | Finished bulk tasks kept in memory for polling. |
| `JOB_SEARCH_MAX_RUNNING_TASKS` | `2` | Background tasks allowed to run at once. Starting another returns `429`; a task that includes a job already being processed returns `409`. |

Configured paths are resolved at startup; an existing path of the wrong type (for
example a file where a directory is expected) stops startup with exit code `2`.
| `JOB_SEARCH_ALLOW_REMOTE` | `0` | Set `1` to allow a non-loopback `JOB_SEARCH_HOST`. The API has no authentication, so only do this on a trusted network. |
| `JOB_SEARCH_MAX_REQUEST_BYTES` | `1048576` | Largest accepted request body; larger requests get `413`. |
| `JOB_SEARCH_HTTP_MAX_RESPONSE_BYTES` | `5242880` | Largest job-board or posting response the app reads; larger responses fail with `http_response_too_large`. |
| `JOB_SEARCH_DEBUG` | `0` | Flask debug mode. Refused with a non-loopback host because the debugger allows remote code execution. |

Invalid values stop startup with exit code `2` and a message naming the variable.

The app is served by the Werkzeug development server and is meant for local,
single-user use only. It refuses to bind to a non-loopback address unless
`JOB_SEARCH_ALLOW_REMOTE=1` is set.

## Long-Running Work

Decision (T-27): work that calls Codex or runs a whole search happens in
background tasks, not inside HTTP requests. These endpoints return `202` with a
`task`, and the UI polls `GET /api/codex-tasks/<id>` until `status` is
`complete` (the outcome is in `result`) or `error` (see `message` and
`error_code`):

- `POST /api/search/run`
- `POST /api/jobs/<id>/score-gpt`
- `POST /api/jobs/<id>/application-packet/generate`
- `POST /api/jobs/bulk/score-gpt` and `POST /api/jobs/bulk/application-packets/generate`
- `POST /api/jobs` returns `201` with the saved job and, when Codex is
  available, a `score_task` for the automatic score.

Preconditions (job exists, Codex available, no packet yet) are still checked
synchronously, so those errors come back immediately as `4xx`.

Upper bound for synchronous handlers: apart from local database and file work,
the only outbound call made inside a request is the posting scrape in
`POST /api/jobs` and `POST /api/jobs/<id>/scrape`. Each fetch has an overall
deadline of `JOB_SEARCH_HTTP_TIMEOUT_SECONDS x 6` (initial request plus up to 5
redirects; 180 seconds by default), after which it fails with
`http_fetch_deadline_exceeded`.

## Accessibility

- Every form control has an associated label; the typed DELETE/PURGE
  confirmations use a labelled `<dialog>` instead of `prompt()`.
- Errors appear in a dismissible `role="alert"` region instead of `alert()`.
- Scores show a text label (strong, borderline, weak) beside the colour.
- The active page button carries `aria-current="page"`; table rows open through a
  real button in the name cell, and the selected row is marked `aria-current`.

Verified 2026-09-26 with axe-core (WCAG 2.0/2.1 A and AA rules, via Playwright
and Chrome) on the jobs page, jobs page with detail, companies page, queries
page, confirmation dialog, and error region: 0 violations. Keyboard-only runs
opened a job, switched pages, and completed a typed DELETE confirmation.

## Data

All generated data is local and owned by the user running the app. Nothing
is uploaded except the Codex prompts sent through your Codex CLI.

| Location | Contents | Sensitivity | Retention |
| --- | --- | --- | --- |
| `job_search.sqlite3` (`JOB_SEARCH_DB_PATH`) | Tracked jobs, CRM notes and interactions, company interest, searches, discoveries, settings | Personal job-search data | Kept until you delete jobs in the UI (single delete or the confirmed `PURGE`). |
| `captures/` (`JOB_SEARCH_CAPTURE_DIR`) | Replayable HTTP responses and Codex requests/responses. Codex prompts include your master resume and Career Manual excerpts. `Set-Cookie`, `Cookie`, and `Authorization` headers are never stored. Successful responses are `<digest>.json`; each failed call is kept as its own `<digest>.failed.<timestamp>.json`. | Personal (resume content) | Grows until pruned with `prune-captures --older-than DAYS --yes`, which moves old captures into `captures/archive/captures-<UTC>.tar.gz` (verified before originals are removed); nothing is deleted outright. The command is a dry run without `--yes`. |
| `logs/` (`JOB_SEARCH_LOG_DIR`) | Structured event and API logs | Operational; contains job URLs and titles | Rotated at `JOB_SEARCH_LOG_MAX_BYTES` into timestamped `.gz` archives that the app never deletes. Remove old archives manually if disk space matters. |
| `<workspace>/applications/` | Generated application packets (Markdown and DOCX) | Personal | Kept indefinitely; manage the folders yourself. |
| `.env` | Local configuration | May contain local paths | Kept; edited by `run.sh` and the Configuration panel. |

Decision (T-31): rotated logs are archived, not deleted, because logs are
evidence. `JOB_SEARCH_LOG_BACKUP_COUNT` was removed with this change.

Decision (T-44): capture pruning follows the same rule. Captures include Codex
prompts and responses (model-call records) and HTTP fetches (tool-call records),
so `prune-captures` archives them into a compressed tarball instead of deleting
them. Remove old archives manually if disk space matters.

## Response Size

List responses (`/api/state`, and the job lists returned after delete, purge,
and threshold changes) carry job summaries only; posting text, notes,
rationales, and scorecards come from `GET /api/jobs/<id>`. Job listings accept
`limit` (default 2000, maximum 5000) and `offset`, and `/api/state` reports
`jobs_total`. Task entries in `/api/state` and `/api/codex-tasks` omit `result`;
fetch `GET /api/codex-tasks/<id>` for it. With 1,000 jobs, `/api/state` stays under 1 MB (checked in
`tests/test_response_bounds.py`). GET requests never create directories; the
`applications/` folder is created at startup.

## HTTP API

All endpoints are local JSON APIs used by the UI. State-changing requests must
send `Content-Type: application/json` (or no body) from an allowed host and
origin. Every error response is `{"error": ..., "request_id": ...}`; common
error statuses for all routes are `400` (validation), `403` (host/origin),
`413`, `415`, and `500`. The table lists route-specific statuses.
"Task" means the response is `202` with a background `task` to poll via
`GET /api/codex-tasks/<task_id>`.

| Method | Path | Body / query | Success | Other statuses |
| --- | --- | --- | --- | --- |
| GET | `/` | — | `200` HTML UI | — |
| GET | `/api/state` | query `include_filtered=1`, `limit` (1-5000, default 2000), `offset` | `200` jobs (summaries), `jobs_total`, companies, searches, runs, discoveries, packets, tasks, settings, masked config | — |
| GET | `/api/metrics` | — | `200` blame-metric counters | — |
| GET | `/api/jobs/<job_id>` | — | `200` full job with notes and interactions | `404` |
| POST | `/api/jobs` | `url`, `pipeline` (required); `company`, `title`, `location`, `status`, `posting_text`, `notes`, `force_refresh` | `201` job, `scrape_error`, `score_error`, `score_task` | `409` duplicate URL (body includes the existing `job`) |
| DELETE | `/api/jobs/<job_id>` | `confirm: "DELETE"` | `200` `deleted_job_id`, jobs | `404` |
| POST | `/api/jobs/<job_id>/scrape` | `force_refresh` (default true) | `200` job, scraped fields | `404`, `502` fetch failure |
| POST | `/api/jobs/<job_id>/score-gpt` | — | `202` task (result `raw_score`) | `404`, `429`, `503` scoring disabled or Codex unavailable |
| POST | `/api/jobs/<job_id>/score-user` | `scorecard` (rubric fields 0-10), `total_score` (0-100, optional), `user_rationale` | `200` job | `404` |
| POST | `/api/jobs/<job_id>/interactions` | `occurred_on` (YYYY-MM-DD, required), `person_name`, `person_role`, `channel`, `summary`, `notes_to_self`, `next_step` | `201` job | `404` |
| POST | `/api/jobs/<job_id>/notes` | `note` (required) | `201` job | `404` |
| POST | `/api/jobs/<job_id>/status` | `status` (one of the job statuses) | `200` job | `404` |
| POST | `/api/admin/purge-jobs` | `confirm: "PURGE"` | `200` `deleted_jobs`, jobs, discoveries | — |
| GET | `/api/codex-tasks` | — | `200` recent task summaries (no `result`) | — |
| GET | `/api/codex-tasks/<task_id>` | — | `200` task (`status`, `items`, `result`, `message`, `error_code`) | `404` |
| POST | `/api/jobs/bulk/score-gpt` | `job_ids` (1-1000 integers) | `202` task | `409` job already in a task, `429`, `503` |
| POST | `/api/jobs/bulk/application-packets/generate` | `job_ids` (1-1000 integers) | `202` task | `409`, `429`, `503` |
| GET | `/api/application-packets` | — | `200` packet folders with associations | — |
| POST | `/api/jobs/<job_id>/application-packet/generate` | — | `202` task (result `packet`) | `404`, `409` already associated, `429`, `503` |
| POST | `/api/jobs/<job_id>/application-packet/attach` | `path` (under `applications/`) | `200` job, packets | `404` |
| GET | `/api/jobs/<job_id>/application-packet/content` | query `file` (a `.md` name, max 255 chars) | `200` Markdown content and file list | `404` |
| GET | `/api/jobs/<job_id>/application-packet/render` | query `file` | `200` HTML preview | `404` |
| GET | `/api/companies/<company_id>` | — | `200` company with matching jobs | `404` |
| POST | `/api/companies` | `company` (required), `status`, `interest_score` (0-100), `rationale`, `notes`, `next_step`, `contacts` | `201` company, companies (upserts by normalized name) | — |
| POST | `/api/companies/<company_id>` | any company fields (omitted fields are kept) | `200` company, companies | `404` |
| POST | `/api/search/run` | `force_refresh` | `202` task (result `run`) | `409` a search is already running (body includes that `task`; the UI follows it), `429` |
| POST | `/api/search/queries` | `board` (`linkedin`/`indeed`), `keywords` (required), `pipeline`, `location`, `criteria`, `enabled` | `201` search queries | — |
| POST | `/api/search/queries/<query_id>` | any query fields | `200` search queries | `404` |
| POST | `/api/settings` | `gpt_threshold`, `user_threshold` (0-100), `codex_model` | `200` settings, jobs | — |
| POST | `/api/config` | `CODEX_CLI_PATH`, `CODEX_MODEL`, `JOB_SEARCH_ENABLE_GPT_SCORING`, `JOB_SEARCH_USE_CAPTURE_CACHE` | `200` masked config and settings | — |

## Errors, Logs, And Troubleshooting

- State-changing requests must send JSON (`Content-Type: application/json`) or
  no body at all; malformed JSON returns `400`.
- API errors return JSON `{"error": ..., "request_id": ...}` with `400`
  (validation), `403` (disallowed host or cross-origin request), `404`, `409` (conflict with current state), `413` (body too large), `429` (too many background tasks running),
  `415` (non-empty body that is not `application/json`), `503` (Codex CLI, Codex scoring, or Pandoc unavailable), `502`
  (Codex/Pandoc failure), or `500`. A `500` never includes internal details;
  search the logs for its `request_id`.
- The UI is `static/index.html` plus `static/app.js` and `static/app.css`, with no
  inline script or event-handler attributes, so the CSP uses `script-src 'self'`.
- Every response carries `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.
- Every log line carries a `process_run_id` that identifies one app run
  (logged with the PID in `app_started`), so events from separate runs can be
  told apart in the rotated logs.
- Every response has an `X-Request-ID` header. Search runs and bulk tasks log a
  `correlation_id` of `search-run-<id>` or `task-<id>`.
- Major operations (search runs, scoring, packet generation and attach, manual
  add, re-scrape, delete, purge, query refinement, config updates) emit
  `<name>_started`, `<name>_succeeded`, and `<name>_failed` events. Startup and
  shutdown emit `app_started` and `app_stopped` (with `outcome`).
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
Saved values are written (quoted) to `job-search-tool/.env`, applied in memory to
the running app without modifying the process environment, and displayed only in
masked form.

Codex invocations default to a 270-second timeout. Override this when needed by
setting `CODEX_CLI_TIMEOUT_SECONDS` in `job-search-tool/.env`.

Application packet generation uses one read-only, JSON-only Codex drafting call.
The app provides the captured job posting, the compact application-CV rules, and
the master resume as context, then validates and writes `Job-Brief.md` and
`CV.md` itself. Pandoc generates DOCX only for those deliverables locally. This
removes repository exploration and filesystem/document work from the Codex
invocation. The Codex drafting call follows the repository's `AGENTS.md`
guidance; the app appends verified model/date attribution after the response,
so attribution does not trigger a second drafting call. Cover letters are
separate, optional deliverables and are not generated by the default packet
workflow. Set `CODEX_MODEL` only to override the Codex CLI default.

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

Only successful outcomes are replayed: a `2xx` HTTP response, or a Codex call
that exited `0`, stored as `<digest>.json`. Failed calls are written to their own
`<digest>.failed.<UTC timestamp>.json` files, which are never overwritten, and the
next request makes a live call; a failure never replaces the last good capture. Captures are written atomically, and a
failed capture write is logged (`capture_write_failed`) without affecting the
request.

Manual searches include a `Force refresh` checkbox. When checked, the search
bypasses replay and makes live LinkedIn, Indeed, and Codex CLI requests, then
writes the fresh responses back to captures. (When scheduled searches are
re-enabled, they always force refresh.)

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


## Search Profile

Everything specific to the candidate lives in a JSON search profile, not in code:
the candidate name, pipelines and their keywords, the sales-role exclusion, the
target level (label, reference text, and title patterns for at-or-above and
below), the compensation floor, the home-metro and US/non-US location terms, the
downlevel exception score, and the scoring and query-refinement instructions sent
to Codex.

`profile.example.json` ships with the current values. To customize, copy it to
`<workspace>/job-search-profile.json` (or anywhere, and set
`JOB_SEARCH_PROFILE_PATH`). The profile is validated at startup; an invalid
profile stops the app with exit code `2` and a message naming the bad field.

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

`JOB_SEARCH_INTERVAL_SECONDS` will set the cadence once the scheduler is re-enabled; it has no effect today.

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
- Codex output is schema-validated before it is saved: `total_score` is clamped
  to 0-100, rubric values to 0-10, unknown rubric keys are dropped, and text is
  truncated (each adjustment is logged as `codex_output_normalized`). Unusable
  payloads, such as a non-object scorecard or non-string refinement keywords,
  are rejected with a specific error code and nothing is persisted.
- Codex score threshold defaults to `40`.
- User score threshold defaults to `60`.
- A job is filtered when it is downlevel, when user score is below threshold, or when Codex scoring is enabled and Codex score is below threshold.
- Codex scoring includes recent user-scored examples as calibration context, so the model can adapt to Eric's preferences over time.
- Thresholds are editable in the UI and persisted in SQLite.
- The tracked jobs table includes fine-grained table filters for pipeline, source, filtered/downlevel visibility, text search, and included statuses. By default it hides filtered/downlevel rows plus terminal `rejected` and `declined` statuses.
- Company interest is tracked independently from individual roles. Jobs are matched to a company by normalized name (case and
  punctuation ignored, so "Acme, Inc." matches "Acme Inc"). Company records can store interest status, interest score, rationale, contacts, notes, and next step while still showing matching tracked jobs for context.
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
- Run saved LinkedIn and Indeed searches on demand (scheduled daily runs are currently disabled).
- Hide downlevel discoveries by default while retaining them in tracked jobs.
