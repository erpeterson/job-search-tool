# Standards Compliance Tasks

Work needed to bring the codebase into full compliance with [AGENTS.md](AGENTS.md).
Findings come from a review of commit `6aab6ba`. At that commit, `ruff check`, `ruff format --check`,
and `pytest` all pass, with 137 tests and 95% coverage.

**Priority:** P1 = security or data-integrity risk; fix first. P2 = clear standards violation.
P3 = hygiene or completeness.

**Conventions for every task:**

- Include tests for the change, with at least one failure-path test and assertion messages.
- Every new caught exception must call `record_exception` with a new, stable error code.
- Update README.md and `.env.example` whenever behavior, configuration, or the API changes.

## Summary

| ID | Title | Area | Priority | Status |
| --- | --- | --- | --- | --- |
| T-1 | Validate `Host` and `Origin` headers | Security | P1 | Done |
| T-2 | Refuse unsafe bind and debug combinations | Security | P1 | Done |
| T-3 | Restrict `CODEX_CLI_PATH` updates from the web UI | Security | P1 | Done |
| T-4 | Reject malformed or non-JSON request bodies; cap request size | Security / Validation | P1 | Done |
| T-5 | Close SSRF gaps in the HTTP client | Security | P1 | Done |
| T-6 | Sanitize board-sourced URLs before rendering them as links | Security | P1 | Done |
| T-7 | Add HTTP security headers | Security | P2 | Done |
| T-8 | Stop returning raw exception text and internal paths to clients | Security / Errors | P2 | Done |
| T-9 | Add security linting and a dependency vulnerability audit | Tooling | P2 | Open |
| T-10 | Pin transitive dependencies | Dependencies | P2 | Open |
| T-11 | Record the swallowed `ConflictError` in `api_create_job` | Telemetry | P1 | Done |
| T-12 | Stop leaving search runs stuck in `running` after a failure | Errors / Integrity | P1 | Done |
| T-13 | Add top-level exception handling to background threads | Errors / Telemetry | P1 | Done |
| T-14 | Do not replay failed captures as successes; make capture writes safe | Integrity | P1 | Done |
| T-15 | Add a timeout and error handling to the Pandoc subprocess | Errors / Performance | P2 | Open |
| T-16 | Emit start, success, and failure events for all major operations | Observability | P2 | Open |
| T-17 | Tag non-request events with a process run ID | Observability | P3 | Open |
| T-18 | Map `DependencyUnavailableError` to a correct HTTP status | Errors | P3 | Open |
| T-19 | Split `RuntimeSettings` and stop mutating `os.environ` | Architecture | P2 | Open |
| T-20 | Move orchestration out of Flask routes | Architecture | P2 | Open |
| T-21 | Move domain decisions out of the job-board adapter | Architecture | P3 | Open |
| T-22 | Make hardcoded paths, timeouts, and limits configurable | Configuration | P2 | Open |
| T-23 | Move the user-specific search profile out of source code | Configuration | P2 | Open |
| T-24 | Fix `.env.example` and `run.sh` configuration handling | Configuration / CLI | P2 | Open |
| T-25 | Close request-validation gaps | Validation | P2 | Open |
| T-26 | Validate Codex model output against a schema | Validation | P1 | Done |
| T-27 | Move long-running work out of request handlers | Performance | P2 | Open |
| T-28 | Cap concurrent background tasks | Performance | P2 | Open |
| T-29 | Refactor N+1 queries into batched queries and index the company join | Performance | P2 | Open |
| T-30 | Bound API response sizes | Performance | P3 | Open |
| T-31 | Define lifecycle and retention for generated data | Data handling | P2 | Open |
| T-32 | Write `.env` atomically | Data handling | P3 | Open |
| T-33 | Resolve the disabled scheduler | Code standards | P3 | Open |
| T-34 | Fix accessibility gaps in the web UI | Accessibility | P2 | Open |
| T-35 | Extract frontend script and remove inline handlers | Security / Frontend | P3 | Open |
| T-36 | Add assertion messages to bare test asserts | Tests | P2 | Open |
| T-37 | Cover untested boundary and failure paths | Tests | P3 | Open |
| T-38 | Document the HTTP API and fix README inaccuracies | Documentation | P3 | Open |

---

## Security

### T-1 — Validate `Host` and `Origin` headers (P1)

- **Standard:** OWASP: broken access control, insecure design.
- **Where:** [job_search/web/app.py:47](job_search/web/app.py#L47) (`create_app`)
- **Problem:** The app has no authentication and relies on binding to `127.0.0.1`. That does not stop
  DNS rebinding. A malicious site can rebind its hostname to `127.0.0.1` and call every API, including
  `POST /api/config`, which changes the executable the app runs (see T-3). No `Host` or `Origin`
  check is done anywhere.
- **Action:**
  - Add a `before_request` hook that rejects requests whose `Host` is not in an allowlist.
    Default the allowlist to `127.0.0.1:<port>` and `localhost:<port>`. Make it configurable with
    `JOB_SEARCH_ALLOWED_HOSTS`.
  - For state-changing methods (`POST`, `PUT`, `PATCH`, `DELETE`), reject requests whose `Origin`
    header is present and not an allowed origin. Return `403`.
- **Done when:** Tests show that a request with `Host: evil.example` gets `403`, and that a
  cross-origin `POST` gets `403`. Both rejections are logged with an error code.

### T-2 — Refuse unsafe bind and debug combinations (P1)

- **Standard:** OWASP: security misconfiguration.
- **Where:** [job_search/config.py:79-97](job_search/config.py#L79-L97),
  [job_search/cli.py:66-67](job_search/cli.py#L66-L67)
- **Problem:**
  - `JOB_SEARCH_HOST=0.0.0.0` exposes the unauthenticated API, including the code-execution path in
    T-3, to the network.
  - `JOB_SEARCH_DEBUG=1` enables the Werkzeug interactive debugger, which allows remote code
    execution, on whatever host the app is bound to.
  - The app always serves through the Werkzeug development server.
- **Action:**
  - In `AppConfig.from_env`, raise `ConfigurationError` when the host is not a loopback address,
    unless `JOB_SEARCH_ALLOW_REMOTE=1` is set.
  - Always refuse `debug=True` with a non-loopback host.
  - Document that the development server is only for local use. Optionally, serve with a production
    WSGI server such as `waitress`.
- **Done when:** Tests show that `JOB_SEARCH_HOST=0.0.0.0` without the opt-in exits with code `2`.

### T-3 — Restrict `CODEX_CLI_PATH` updates from the web UI (P1)

- **Standard:** OWASP: injection, insecure design.
- **Where:** [job_search/config.py:156-171](job_search/config.py#L156-L171) (`validate_updates`),
  [job_search/data/codex_client.py:86-97](job_search/data/codex_client.py#L86-L97)
- **Problem:** `POST /api/config` accepts any string as `CODEX_CLI_PATH`, saves it to `.env`, and the
  next scoring call runs it as a subprocess. Anyone who can reach the API can run an arbitrary
  executable.
- **Action:** Choose one of these:
  - (a) Remove `CODEX_CLI_PATH` from the keys the UI can edit, so it is set only through `.env` or
    `run.sh --codex-cli`.
  - (b) Validate the value: it must be an absolute path to an existing executable file whose
    basename is `codex`, or the bare name `codex` resolved through `PATH`.

  Record the choice in README.
- **Done when:** A test shows that `POST /api/config` with `CODEX_CLI_PATH=/bin/sh` is rejected with
  `400` and does not change `.env`.

### T-4 — Reject malformed or non-JSON request bodies; cap request size (P1)

- **Standard:** Input validation; OWASP: CSRF-adjacent insecure design; Performance: unbounded memory.
- **Where:** [job_search/web/validation.py:16-22](job_search/web/validation.py#L16-L22) (`json_body`),
  [job_search/web/app.py:48](job_search/web/app.py#L48)
- **Problem:**
  - `request.get_json(silent=True)` turns malformed JSON, and any non-JSON `Content-Type`, into `{}`.
    So a cross-site `text/plain` form post to `/api/search/run`, `/api/jobs/<id>/score-gpt`, or
    `/api/jobs/<id>/application-packet/generate` runs the action with defaults and spends Codex
    credits.
  - `MAX_CONTENT_LENGTH` is not set, so request bodies are unbounded.
- **Action:**
  - Return `415` when a request with a non-empty body has a non-JSON `Content-Type`.
  - Return `400` (`request_body_malformed_json`) when the JSON cannot be parsed.
  - Treat only an empty body as `{}`.
  - Set `app.config["MAX_CONTENT_LENGTH"]` from a configurable value with a default of 1 MB. The
    200 KB posting-text limit plus overhead fits within that.
- **Done when:** Tests cover malformed JSON (`400`), `text/plain` (`415`), and an oversized body (`413`).

### T-5 — Close SSRF gaps in the HTTP client (P1)

- **Standard:** OWASP: SSRF; Performance: unbounded memory.
- **Where:** [job_search/domain/jobs.py:17-41](job_search/domain/jobs.py#L17-L41) (`validate_posting_url`),
  [job_search/data/http_client.py:50](job_search/data/http_client.py#L50)
- **Problem:**
  - `validate_posting_url` checks only the literal hostname.
  - `requests.get` follows redirects by default, so a public URL that redirects to
    `http://127.0.0.1:5050/...` or to `169.254.169.254` gets fetched.
  - Hostnames are never resolved, so a DNS name that points to a private address passes.
  - Response bodies are read fully into memory with no size cap.
- **Action:**
  - Call `get(..., allow_redirects=False)` and follow up to 5 redirects manually, re-running URL
    validation on each `Location`.
  - Resolve the hostname with `socket.getaddrinfo` and reject any address that is not
    `ipaddress.ip_address(...).is_global`.
  - Stream the response (`stream=True`) and stop reading after a configurable byte limit, such as
    5 MB.
- **Done when:** Tests show that a redirect to a loopback address is refused, a hostname that
  resolves to `10.0.0.1` is refused, and an oversized body is truncated or rejected. Each case
  produces a distinct error code.

### T-6 — Sanitize board-sourced URLs before rendering them as links (P1)

- **Standard:** OWASP: injection (XSS).
- **Where:** [job_search/data/job_boards.py:147](job_search/data/job_boards.py#L147),
  [job_search/data/job_boards.py:179-191](job_search/data/job_boards.py#L179-L191);
  [job_search/web/static/index.html:916](job_search/web/static/index.html#L916), and lines 1034,
  1065, and 1180 of the same file
- **Problem:** URLs scraped from LinkedIn or Indeed HTML are stored without a scheme check. The UI
  renders them as `<a href="${escapeAttr(url)}">`. Escaping does not block `javascript:` URLs, so a
  crafted listing can run script when the user clicks "posting". Only manually entered URLs go
  through `validate_posting_url`.
- **Action:**
  - In the board parsers, drop any result whose URL is not absolute `http` or `https`.
  - In the frontend, add a `safeHref(url)` helper that returns `#` for any other scheme, and use it
    for every `href`.
  - Add `rel="noopener noreferrer"` to every `target="_blank"` link.
- **Done when:** A parser test shows that a `javascript:` href is dropped, and every `href` in
  `index.html` goes through `safeHref`.

### T-7 — Add HTTP security headers (P2)

- **Standard:** OWASP: security misconfiguration.
- **Where:** [job_search/web/app.py:59-72](job_search/web/app.py#L59-L72) (`after_request`)
- **Problem:** Responses have no `Content-Security-Policy`, `X-Content-Type-Options`,
  `X-Frame-Options` (or `frame-ancestors`), or `Referrer-Policy` header.
- **Action:**
  - Set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and
    `Referrer-Policy: no-referrer`.
  - Set this CSP: `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'`.
    After T-35 is done, remove `'unsafe-inline'` from `script-src`.
- **Done when:** A test checks for the headers on `/`, on an API response, and on an error response.

### T-8 — Stop returning raw exception text and internal paths to clients (P2)

- **Standard:** Web exception handlers must not leak implementation details; Security.
- **Where:**
  - [job_search/data/packet_store.py:31-33](job_search/data/packet_store.py#L31-L33): Pandoc stderr
    goes into the message that is returned with `502`.
  - [job_search/data/codex_client.py:115-117](job_search/data/codex_client.py#L115-L117): the CLI
    path is in the client message.
  - [job_search/domain/scoring.py:116-119](job_search/domain/scoring.py#L116-L119) and
    [job_search/domain/packets.py:100-106](job_search/domain/packets.py#L100-L106): the CLI path is in
    the client message.
  - [job_search/domain/jobs.py:92](job_search/domain/jobs.py#L92): `scrape_error = str(exc)` is
    returned in the API response and saved into job notes.
  - [job_search/domain/search.py:187](job_search/domain/search.py#L187) and
    [job_search/domain/search.py:213](job_search/domain/search.py#L213): raw `{exc}` text is saved in
    `search_runs.message` and shown in the UI.
- **Action:**
  - Give `AppError` an optional `detail` field for diagnostics. It is logged by `record_exception`
    but never serialized to the client.
  - Rewrite the messages listed above as user-safe text, such as "Pandoc conversion failed; see logs
    (request ID …)", and move the specifics into `detail`.
  - Replace `str(exc)` in API payloads and persisted messages with the error code plus a generic
    message.
- **Done when:** Tests show that a Pandoc failure response does not contain stderr text, and that a
  failed scrape does not put the exception text into `notes` or into the response.

### T-9 — Add security linting and a dependency vulnerability audit (P2)

- **Standard:** Use static-analysis tools; OWASP: vulnerable dependencies; document suppressions.
- **Where:** [pyproject.toml:14](pyproject.toml#L14), [requirements-dev.txt](requirements-dev.txt)
- **Problem:**
  - Ruff selects `E,F,W,I,B,UP,N,SIM` but no security rules.
  - No tool checks dependencies for known CVEs.
  - `tests/conftest.py` has `# noqa: A002`, but rule `A` is not enabled, so the suppression does
    nothing.
- **Action:**
  - Add `"S"` (flake8-bandit) and `"A"` to `select`.
  - Fix the new findings, or suppress each one with a line-level `# noqa: <rule> - <reason>`. The
    f-string SQL in `database.py` and `repositories.py` needs `S608` suppressions that explain why it
    is safe. `subprocess` usage needs `S603`/`S607` reasons.
  - Add a pinned `pip-audit` to `requirements-dev.txt` and a README step:
    `.venv/bin/pip-audit -r requirements.txt`.
- **Done when:** `ruff check` passes with `S` and `A` enabled, every suppression has a reason, and
  `pip-audit` runs clean or its findings are documented.

### T-10 — Pin transitive dependencies (P2)

- **Standard:** Pin dependency versions for replayability.
- **Where:** [requirements.txt](requirements.txt), [run.sh:127-131](run.sh#L127-L131)
- **Problem:** Only the four direct dependencies are pinned. Werkzeug, Jinja2, urllib3, soupsieve,
  certifi, and others resolve to whatever version is current at install time.
- **Action:**
  - Generate a fully pinned lock file with hashes, for example
    `pip-compile --generate-hashes -o requirements.lock requirements.txt`.
  - Install from the lock file in `run.sh` and in the README development steps.
  - Do the same for the dev requirements.
- **Done when:** Two fresh installs from the lock file produce identical `pip freeze` output.

## Error Handling And Telemetry

### T-11 — Record the swallowed `ConflictError` in `api_create_job` (P1)

- **Standard:** Every caught exception must emit a blame metric and a unique log line.
- **Where:** [job_search/web/routes.py:76-79](job_search/web/routes.py#L76-L79)
- **Problem:** The route catches `ConflictError` and returns `409` without calling
  `record_exception`. This is the only catch site in the codebase that emits no telemetry. The route
  also does a second lookup, which is orchestration in the presentation layer.
- **Action:**
  - Add an `existing_job_id` attribute to `ConflictError`, or create a `DuplicateJobError`
    subclass. Raise it from `JobService.create_manual`.
  - Remove the `try`/`except` from the route, and have the global `AppError` handler include the
    existing job in the body. Alternatively, keep the catch and call `record_exception` with the
    error code `manual_job_duplicate_url`.
- **Done when:** `test_duplicate_url_conflicts` also asserts that the counter
  `blame.job_url_already_tracked` (or the chosen code) incremented.

### T-12 — Stop leaving search runs stuck in `running` after a failure (P1)

- **Standard:** Emit failure events for major operations; do not leave inconsistent state.
- **Where:** [job_search/domain/search.py:141-169](job_search/domain/search.py#L141-L169),
  [job_search/domain/search.py:269](job_search/domain/search.py#L269),
  [job_search/domain/scheduler.py:42-51](job_search/domain/scheduler.py#L42-L51)
- **Problem:**
  - `run()` commits a `search_runs` row with `status='running'`, then processes results. If
    `self._scoring.score(...)` raises for one discovered result (for example a Codex timeout, which
    raises `ExternalServiceError`), the whole run aborts and the row stays `running` forever.
  - One bad result also kills every remaining query.
  - The scheduler's failure path inserts a new `error` row instead of updating the original one.
- **Action:**
  - In `_process_result`, catch `AppError` from scoring, call `record_exception` with the error code
    `search_result_scoring_failed`, track the result without a score (the same path used when Codex
    is unavailable), and continue.
  - In `run()`, wrap the loop in `try`/`except`. On an unexpected failure, update the run to
    `status='error'` with a safe message, then re-raise. Add `SearchRepository.fail_run(run_id, ...)`.
  - Change the scheduler to rely on that instead of `record_failed_run`.
- **Done when:** A test where scoring raises for one result shows that the run completes and the
  other results are tracked. A test where the run itself fails shows the row marked `error`.

### T-13 — Add top-level exception handling to background threads (P1)

- **Standard:** Top-level exception handling; blame metrics for every failure.
- **Where:** [job_search/domain/tasks.py:95-96](job_search/domain/tasks.py#L95-L96) and
  [job_search/domain/tasks.py:149-170](job_search/domain/tasks.py#L149-L170) (`BulkOperations._run`),
  [job_search/domain/scheduler.py:53-56](job_search/domain/scheduler.py#L53-L56) (`_loop`)
- **Problem:**
  - `_run_item` catches per-item errors, but any exception elsewhere in `_run` (registry updates,
    `log_event`) kills the thread. Python's default `threading.excepthook` prints the exception to
    stderr with no structured event and no blame metric, and the task stays `running` in the UI.
  - In the scheduler, if `record_failed_run` raises inside the `except` block, the scheduler thread
    dies silently.
- **Action:**
  - Wrap each thread target in a function that catches `Exception`, calls `record_exception`
    (`background_task_crashed` or `scheduler_loop_crashed`), and marks the task `error` with a
    generic message.
  - Install a `threading.excepthook` in `cli.run` that routes to `record_exception` with the error
    code `thread_unhandled_exception`.
- **Done when:** A test with a registry `update` that raises shows the task ends in `error` and the
  blame counter increments.

### T-14 — Do not replay failed captures as successes; make capture writes safe (P1)

- **Standard:** Integrity failures; do not swallow exceptions; preserve evidence.
- **Where:** [job_search/data/codex_client.py:60-63](job_search/data/codex_client.py#L60-L63) and
  [job_search/data/codex_client.py:130-158](job_search/data/codex_client.py#L130-L158),
  [job_search/data/http_client.py:42-44](job_search/data/http_client.py#L42-L44) and
  [job_search/data/http_client.py:64-74](job_search/data/http_client.py#L64-L74),
  [job_search/data/captures.py:52-68](job_search/data/captures.py#L52-L68)
- **Problem:**
  - Captures are written in a `finally` block for failed calls too.
  - On replay, `call_json` ignores `returncode` and `error_type`. A Codex call that exited non-zero
    but printed stdout is replayed as a successful `CodexResult`, and a timeout is replayed as empty
    output. HTTP network errors are replayed as `None`-status failures until someone force-refreshes.
  - If `captures.write` raises (disk full, permissions), the `finally` block replaces the original
    exception, and the write failure is never recorded.
  - Writes are not atomic, so a crash mid-write leaves a corrupt capture.
- **Action:**
  - In both clients, treat a capture as a cache hit only when it records a successful outcome:
    `returncode == 0` for Codex, a 2xx status with no `error_type` for HTTP. Otherwise log
    `capture_replay_skipped_failed` and make a live call.
  - Keep writing failure captures, because they are evidence.
  - Wrap the `captures.write` call in `try`/`except OSError` with `record_exception` and the error
    code `capture_write_failed`, so it never masks the original error.
  - Write to a temporary file in the same directory, then `os.replace` it into place.
- **Done when:** Tests show that a non-zero-exit capture is not replayed, and that a failing
  `captures.write` does not replace the original exception.

### T-15 — Add a timeout and error handling to the Pandoc subprocess (P2)

- **Standard:** Avoid blocking and unbounded work; handle and record exceptions.
- **Where:** [job_search/data/packet_store.py:25-30](job_search/data/packet_store.py#L25-L30)
- **Problem:** `subprocess.run` for Pandoc has no `timeout`, so a hung Pandoc blocks the request or
  worker thread forever. `OSError` (the binary vanished, or is not executable) is not caught, so it
  reaches the global handler as a generic `500`.
- **Action:**
  - Add a `PANDOC_TIMEOUT_SECONDS` setting with a default of 120.
  - Catch `subprocess.TimeoutExpired` and `OSError`. For each, call `record_exception`
    (`pandoc_timeout`, `pandoc_launch_failed`) and raise `ExternalServiceError`.
- **Done when:** `test_pandoc_converter_errors` covers the timeout and launch-failure cases.

### T-16 — Emit start, success, and failure events for all major operations (P2)

- **Standard:** Observability: start, success, and failure events for major operations.
- **Where:** [job_search/domain/jobs.py](job_search/domain/jobs.py) (`create_manual`, `rescrape`,
  `delete`, `purge_all`), [job_search/domain/packets.py:222-230](job_search/domain/packets.py#L222-L230)
  (`attach`), [job_search/domain/settings.py:30-38](job_search/domain/settings.py#L30-L38)
  (`update_runtime_config`), [job_search/domain/search.py:336](job_search/domain/search.py#L336)
  (`refine_query`), [job_search/cli.py:47-63](job_search/cli.py#L47-L63) (startup and shutdown)
- **Problem:** Only `search_run`, `codex_score`, and `application_packet_generation` use
  `observability.operation()`. The operations listed above log at most a success event and emit
  nothing on failure. `app_stopped` is not emitted when `serve` raises.
- **Action:**
  - Wrap each listed operation in `with operation("<name>", "<component>", ...)`.
  - In `cli.run`, emit `app_stopped` in a `finally` block with the outcome.
- **Done when:** A test asserts `*_started` and `*_failed` events for at least `create_manual` and
  `purge_all`.

### T-17 — Tag non-request events with a process run ID (P3)

- **Standard:** Carry correlation, request, or run IDs across logs and telemetry.
- **Where:** [job_search/cli.py:47-63](job_search/cli.py#L47-L63),
  [job_search/observability.py:81-91](job_search/observability.py#L81-L91)
- **Problem:** Startup, bootstrap, and scheduler events, and any log emitted outside a request or
  task scope, have no correlation ID. Events from separate app runs cannot be told apart in the
  rotating log.
- **Action:**
  - Generate a `process_run_id` at startup and include it in every event payload, alongside any
    `correlation_id`.
  - Log it in `app_started`.
- **Done when:** Every line in `job-search.log` produced during a test run includes `process_run_id`.

### T-18 — Map `DependencyUnavailableError` to a correct HTTP status (P3)

- **Standard:** Transform exceptions into appropriate HTTP response codes.
- **Where:** [job_search/web/app.py:31-37](job_search/web/app.py#L31-L37)
- **Problem:** "Codex CLI unavailable", "scoring disabled", and "Pandoc missing" return `409 Conflict`,
  but these are not conflicts with resource state.
- **Action:** Map `DependencyUnavailableError` to `503 Service Unavailable`. Update README's status
  list and the affected tests.
- **Done when:** The scoring-disabled tests expect `503`.

## Architecture (SOLID And 3-Tier)

### T-19 — Split `RuntimeSettings` and stop mutating `os.environ` (P2)

- **Standard:** Single responsibility; avoid mutable global state; keep layers separated.
- **Where:** [job_search/config.py:108-176](job_search/config.py#L108-L176),
  [job_search/cli.py:39-44](job_search/cli.py#L39-L44)
- **Problem:** `RuntimeSettings` does four jobs:
  - It reads settings from the process environment.
  - It validates updates, raising the domain `ValidationError` from the config module.
  - It persists updates to `.env`.
  - It writes to `os.environ`, which is process-global mutable state that is shared with every
    thread and child process.

  Reading and writing `.env` also use different code: `cli.load_env_file` reads with `python-dotenv`,
  while `EnvFile` writes raw `KEY=value`. A value containing `#`, quotes, or leading or trailing
  spaces does not round-trip.
- **Action:**
  - Keep the current values in an in-memory, thread-safe `RuntimeSettings` object that is seeded
    from the environment once at startup.
  - Move `validate_updates` into a domain-layer `RuntimeSettingsPolicy`.
  - Keep `EnvFile` as the only data-layer reader and writer of `.env`. Write with
    `dotenv.set_key`, or quote values so the dotenv parser reads them back unchanged.
  - Stop writing to `os.environ`.
- **Done when:** `grep -n "os.environ\[" job_search` finds no writes, and a test shows a value
  containing `#` round-trips through save and reload.

### T-20 — Move orchestration out of Flask routes (P2)

- **Standard:** Presentation must not contain workflow or domain decisions.
- **Where:** [job_search/web/routes.py:99-105](job_search/web/routes.py#L99-L105) (`api_score_gpt`:
  get, check availability, score, get again),
  [job_search/web/routes.py:71-80](job_search/web/routes.py#L71-L80) (conflict lookup; see T-11),
  [job_search/web/routes.py:214-218](job_search/web/routes.py#L214-L218) (duplicate
  `validate_markdown_filename`; `read_document` already validates),
  [job_search/web/routes.py:287-295](job_search/web/routes.py#L287-L295) (two service calls)
- **Problem:** Some handlers call several services in sequence and apply domain checks themselves.
- **Action:**
  - Add service methods (for example `ScoringService.score_tracked_job` and
    `SettingsService.apply_runtime_config(payload)`) so that each route parses input, makes one
    service call, and maps the response.
  - Remove the duplicate filename validation from the route.
- **Done when:** No route calls more than one mutating service method, and the existing API tests
  still pass.

### T-21 — Move domain decisions out of the job-board adapter (P3)

- **Standard:** Data access must not make workflow decisions.
- **Where:** [job_search/data/job_boards.py:205-208](job_search/data/job_boards.py#L205-L208)
  (raises `ValidationError` for an unknown board),
  [job_search/data/job_boards.py:241-263](job_search/data/job_boards.py#L241-L263) (user-facing
  defaults such as "Unknown company" and "Job posting from {host}", and the `fallback_posting` policy)
- **Problem:** The adapter decides fallback metadata and validation policy, which belong to the
  domain layer (`JobService`).
- **Action:**
  - Have the adapter return raw scraped fields, with empty strings when a field is not found, and
    raise an adapter error such as `UnsupportedBoardError` for an unknown board.
  - Apply defaults and fallback postings in `JobService`.
- **Done when:** `job_boards.py` contains no user-facing default strings and does not import
  `ValidationError`.

## Configuration

### T-22 — Make hardcoded paths, timeouts, and limits configurable (P2)

- **Standard:** Keep environment-specific values in configuration.
- **Where:**
  - [job_search/config.py:85-94](job_search/config.py#L85-L94): `db_path`, `log_dir`, and
    `capture_dir` are fixed under `app_dir`. The guidance filename
    `20260731-job-search-guidance.md` is date-stamped and hardcoded, as are the career-manual and
    resume paths.
  - [job_search/data/http_client.py:35](job_search/data/http_client.py#L35): HTTP timeout 30 s.
  - [job_search/data/database.py:191](job_search/data/database.py#L191): SQLite timeout 30 s.
  - [job_search/domain/scheduler.py:8](job_search/domain/scheduler.py#L8): `POLL_SECONDS`.
  - [job_search/domain/tasks.py:25](job_search/domain/tasks.py#L25): `max_retained=50`.
- **Action:**
  - Add `JOB_SEARCH_DB_PATH`, `JOB_SEARCH_LOG_DIR`, `JOB_SEARCH_CAPTURE_DIR`,
    `JOB_SEARCH_GUIDANCE_PATH`, `JOB_SEARCH_CAREER_MANUAL_PATH`, `JOB_SEARCH_MASTER_RESUME_PATH`,
    `JOB_SEARCH_HTTP_TIMEOUT_SECONDS`, and `JOB_SEARCH_DB_TIMEOUT_SECONDS`. Use the current values as
    defaults, validate each through `AppConfig.from_env`, and pass them through `build_container`.
  - Sanitize configured paths by resolving them. Reject paths that are not directories or files as
    appropriate.
- **Done when:** `test_defaults` and `test_invalid_values_raise_actionable_errors` cover each new
  variable.

### T-23 — Move the user-specific search profile out of source code (P2)

- **Standard:** Keep environment-specific values in configuration, not hardcoded constants.
- **Where:** [job_search/domain/scoring.py:23](job_search/domain/scoring.py#L23) and
  [job_search/domain/scoring.py:53](job_search/domain/scoring.py#L53) ("Eric Peterson", "Eric's"),
  [job_search/domain/search.py:35](job_search/domain/search.py#L35) and
  [job_search/domain/search.py:348](job_search/domain/search.py#L348),
  [job_search/domain/rules.py](job_search/domain/rules.py) (`PIPELINE_CRITERIA`,
  `ORACLE_IC6_LEVEL_REFERENCE`, `MIN_ANNUAL_COMPENSATION`, `SEATTLE_LOCATION_TERMS`,
  `DOWNLEVEL_HIGH_SCORE_EXCEPTION`), and the Oracle IC6 taxonomy in
  [job_search/domain/levels.py](job_search/domain/levels.py)
- **Problem:** The candidate's name, target level, home metro, compensation floor, and pipelines are
  compiled into business logic. Changing the search requires editing code.
- **Action:**
  - Define a `SearchProfile` dataclass in the domain layer.
  - Load it from a JSON file whose path comes from `JOB_SEARCH_PROFILE_PATH`, with a default under
    the workspace. Validate it at startup and raise `ConfigurationError` with an actionable message.
  - Ship the current values as `profile.example.json`.
  - Inject the profile into `ScoringService`, `SearchService`, the discovery filters, and the level
    estimation.
- **Done when:** `grep -rn "Eric\|Seattle\|Oracle\|200_000" job_search/` returns no matches, and
  tests use a fixture profile.

### T-24 — Fix `.env.example` and `run.sh` configuration handling (P2)

- **Standard:** Configuration; CLI output separation; documentation.
- **Where:** [.env.example](.env.example), [run.sh:158](run.sh#L158) and
  [run.sh:194-196](run.sh#L194-L196)
- **Problem:**
  - `.env.example` sets `JOB_SEARCH_AUTORUN=1`, which the code ignores (see T-33).
  - `.env.example` omits `JOB_SEARCH_DEBUG`, `JOB_SEARCH_INTERVAL_SECONDS`,
    `CODEX_CLI_TIMEOUT_SECONDS`, and `JOB_SEARCH_WORKSPACE_ROOT`.
  - `run.sh` prints `Open http://127.0.0.1:5050` regardless of `JOB_SEARCH_HOST` and
    `JOB_SEARCH_PORT`.
  - `run.sh` writes the "CODEX_CLI_PATH is not configured" warning to stdout instead of stderr.
  - `run.sh` does not check that `python3` is at least 3.12, as `pyproject.toml` requires.
- **Action:**
  - List every supported variable in `.env.example` with a comment, and remove or annotate
    `AUTORUN`.
  - Drop the hardcoded URL echo from `run.sh`; the app already prints the real address.
  - Send warnings to `>&2`.
  - Fail fast with exit code `2` if Python is older than 3.12.
- **Done when:** `run.sh --setup-only` with Python 3.11 exits `2` and prints a clear message on
  stderr.

## Input Validation

### T-25 — Close request-validation gaps (P2)

- **Standard:** Validate all external inputs and return actionable errors.
- **Where:**
  - [job_search/web/routes.py:112](job_search/web/routes.py#L112): `if payload.get("total_score")`
    ignores an explicit `total_score: 0`.
  - [job_search/web/validation.py:136-148](job_search/web/validation.py#L136-L148): `user_scorecard`
    silently clamps and rounds out-of-range values (for example `50` becomes `10`) and ignores
    unknown keys.
  - [job_search/web/validation.py:151-154](job_search/web/validation.py#L151-L154): `occurred_on`
    is not validated as an ISO date and can be empty, even though the column is `NOT NULL` and
    semantically required.
  - [job_search/web/validation.py:124-125](job_search/web/validation.py#L124-L125): a blank company
    name on create silently becomes "Unknown company".
  - [job_search/web/routes.py:203](job_search/web/routes.py#L203) and
    [job_search/web/routes.py:216](job_search/web/routes.py#L216): the `file` query parameter has no
    length limit.
- **Action:**
  - Use `"total_score" in payload`.
  - Reject scorecard values outside 0–10 and unknown rubric keys with a `400` that names the field.
  - Require `occurred_on` in `YYYY-MM-DD` form.
  - Require a company name on create.
  - Limit `file` to 255 characters.
- **Done when:** There is one boundary test per rule, for example `total_score=0` stores 0 and a
  scorecard value of 11 returns `400`.

### T-26 — Validate Codex model output against a schema (P1)

- **Standard:** Validate imported data at system boundaries.
- **Where:** [job_search/domain/scoring.py:144-168](job_search/domain/scoring.py#L144-L168),
  [job_search/domain/search.py:60-86](job_search/domain/search.py#L60-L86) and
  [job_search/domain/search.py:386-398](job_search/domain/search.py#L386-L398)
- **Problem:**
  - Scoring persists `scorecard`, `rationale`, and `level_assessment` from the model unchecked.
    `scorecard` can be a string or a list, values can fall outside 0–10, the total is not clamped to
    0–100, and text is unbounded.
  - In `refine_query`, `clean_text(refined.get("keywords"))` raises `AttributeError` if the model
    returns a non-string such as a list. That exception is not an `AppError`, so it escapes the
    `except AppError` in `_run_query` and aborts the whole search run.
- **Action:**
  - Add `validate_score_payload` and `validate_refinement_payload` functions in the domain layer.
    They should type-check each field, clamp or reject numeric ranges, truncate text to documented
    limits, and raise `ExternalServiceError` with specific codes for unusable payloads.
  - Call them before anything is persisted.
- **Done when:** Tests with a non-dict `scorecard`, `total_score: 250`, and `keywords: ["a"]` each
  produce a recorded, non-fatal outcome.

## Performance

### T-27 — Move long-running work out of request handlers (P2)

- **Standard:** Avoid blocking request handlers on long-running work unless that is intentionally
  designed.
- **Where:** [job_search/web/routes.py:252-264](job_search/web/routes.py#L252-L264)
  (`/api/search/run`), [job_search/web/routes.py:186-191](job_search/web/routes.py#L186-L191)
  (single packet generation), [job_search/web/routes.py:99-105](job_search/web/routes.py#L99-L105)
  (single score), [job_search/web/routes.py:71-80](job_search/web/routes.py#L71-L80) (scrape plus
  auto-score)
- **Problem:** A manual search run makes one Codex call (up to 270 s each) for every discovered
  result, plus one refinement per query, all inside a single HTTP request. The comments say
  "intentionally synchronous", but no time budget is defined, and the request can run for tens of
  minutes.
- **Action:** Pick one of these and record the decision in README:
  - (a) Run these operations through `BackgroundTaskRegistry`, return `202` with a task ID, and
    have the UI poll. This reuses the existing bulk-task polling.
  - (b) Keep them synchronous, but enforce and document a maximum total duration: cap the number of
    results scored per run, and fail with a clear error when the budget is exceeded.
- **Done when:** No request handler can run past a documented upper bound.

### T-28 — Cap concurrent background tasks (P2)

- **Standard:** Avoid unbounded resource growth.
- **Where:** [job_search/domain/tasks.py:63-98](job_search/domain/tasks.py#L63-L98)
- **Problem:** Each bulk request starts a new thread. Only finished tasks are evicted, so the number
  of running tasks and threads is unbounded. Two tasks can process the same job at the same time;
  for packets, the second one fails with `packet_dir_exists`.
- **Action:**
  - Add `max_running`, configurable with a default of 2. When it is reached, raise `ConflictError`
    (or return `429`) with a clear message.
  - Optionally, reject a new task that contains job IDs already queued or running in another task.
- **Done when:** A test shows that starting a third task while two are running is rejected.

### T-29 — Refactor N+1 queries into batched queries and index the company join (P2)

- **Standard:** Avoid N+1 data access patterns.
- **Where:** [job_search/domain/search.py:232-235](job_search/domain/search.py#L232-L235) (for every
  result: a new connection, the level lookup, the URL lookup, and `scoring_inputs`, which re-queries
  calibration examples and all settings), then two more units of work per result.
  [job_search/data/repositories.py:230](job_search/data/repositories.py#L230) and
  [job_search/data/repositories.py:257](job_search/data/repositories.py#L257) join on
  `lower(j.company) = lower(ci.company)` with no usable index.
- **Problem:** Search-run cost grows with the number of results times the number of queries. The
  company list is O(companies × jobs).
- **Action:** Restructure these lookups so each one runs once per run or per query, not once per
  result. Do not drop any lookup: every one must still happen, and results must stay the same.
  - Compute `scoring_inputs` once per `run()` and pass it down.
  - Batch the URL-exists checks per query with one `WHERE url IN (...)`.
  - Add a `normalized_company` column to `jobs`: add it to `_ADDED_COLUMNS`, backfill it in
    `_DATA_MIGRATIONS`, and set it on insert and update. Index it, and join on it.
- **Done when:** A test with a counting connection shows that the calibration query runs once per
  search run, and `EXPLAIN QUERY PLAN` for the company list uses the new index. The existing search
  and company tests still pass unchanged, which shows the discoveries, skip decisions, scores, and
  company job counts are the same as before.

### T-30 — Bound API response sizes (P3)

- **Standard:** Avoid unbounded memory growth.
- **Where:** [job_search/web/routes.py:36-55](job_search/web/routes.py#L36-L55) (`/api/state`),
  [job_search/data/repositories.py:68-73](job_search/data/repositories.py#L68-L73),
  [job_search/data/packet_store.py:49-59](job_search/data/packet_store.py#L49-L59)
- **Problem:**
  - `/api/state` returns every job with its full `posting_text` (up to 200 KB each), scans the
    `applications/` directory, and runs `mkdir` on every call, so a `GET` has a side effect.
  - Delete, purge, and threshold updates also return the full job list.
- **Action:**
  - Use a list projection without `posting_text` and the scorecard JSON for list endpoints, and load
    full detail through `GET /api/jobs/<id>`.
  - Add `limit` and `offset` parameters, or a hard cap, to job listing.
  - Move the `mkdir` into `bootstrap()`.
- **Done when:** The `/api/state` payload for 1,000 seeded jobs stays under a documented size.

## Data Handling

### T-31 — Define lifecycle and retention for generated data (P2)

- **Standard:** Define ownership and lifecycle for generated data; avoid irreversible destructive
  operations without explicit confirmation.
- **Where:** [job_search/data/captures.py](job_search/data/captures.py),
  [job_search/data/http_client.py:67-73](job_search/data/http_client.py#L67-L73),
  [job_search/observability.py:177-189](job_search/observability.py#L177-L189), README
- **Problem:**
  - `captures/` grows without limit. It stores full Codex prompts, which include the master resume
    and the career manual, and full HTTP response headers, which can include `Set-Cookie`.
  - Nothing documents who owns `job_search.sqlite3`, `captures/`, `logs/`, or `applications/`, or
    how long each is kept.
  - Log rotation silently discards the oldest logs, which conflicts with the guidance to never
    delete logs.
- **Action:**
  - Add a README "Data" section: for each location, give its contents, sensitivity, owner, and
    retention.
  - Drop `Set-Cookie` and `Authorization` headers from HTTP captures before writing them.
  - Add a CLI subcommand `job-search prune-captures --older-than DAYS` that lists what it would
    delete and requires `--yes`.
  - Decide whether rotated logs should be archived (for example compressed and kept) instead of
    deleted, and record the decision.
- **Done when:** README documents the lifecycle, a test shows `Set-Cookie` is not stored, and the
  prune command refuses to delete without `--yes`.

### T-32 — Write `.env` atomically (P3)

- **Standard:** Data integrity; avoid destructive partial writes.
- **Where:** [job_search/data/env_file.py:36](job_search/data/env_file.py#L36)
- **Problem:** `write_text` truncates `.env` and then writes it. A crash or a full disk mid-write
  loses all configuration. Two concurrent `POST /api/config` calls can interleave.
- **Action:** Write to a temporary file in the same directory, then `os.replace` it into place.
  Guard `update` with a lock.
- **Done when:** A test simulating a write failure shows the original `.env` intact.

## Code Standards

### T-33 — Resolve the disabled scheduler (P3)

- **Standard:** Prefer the smallest implementation; keep documentation accurate.
- **Where:** [job_search/config.py:28-31](job_search/config.py#L28-L31) (`SCHEDULER_SUPPORTED = False`),
  [job_search/domain/scheduler.py](job_search/domain/scheduler.py)
- **Problem:** The scheduler is disabled in code because of "known bugs" that are not written down
  anywhere. `JOB_SEARCH_AUTORUN` and `JOB_SEARCH_INTERVAL_SECONDS` are parsed but have no effect. The
  scheduler thread is never stopped on shutdown.
- **Action:** Choose one of these:
  - (a) Record the known bugs as their own tasks, fix them, add a `stop()` call on shutdown, and
    remove the gate.
  - (b) Delete the scheduler, its configuration, and its UI state until it is needed.
- **Done when:** No configuration variable is silently ignored.

## Accessibility And Frontend

### T-34 — Fix accessibility gaps in the web UI (P2)

- **Standard:** Semantic markup, keyboard navigation, labeled controls, no color-only status.
- **Where:** [job_search/web/static/index.html](job_search/web/static/index.html)
- **Problem:**
  - Fit scores are styled `.score-good`, `.score-warn`, or `.score-bad` (lines 182–184 and 578),
    which conveys good or bad fit by color only.
  - The current page in the nav (lines 367–369) is shown only by button styling, with no
    `aria-current`.
  - Clickable `<tr>` rows (lines 913, 986, and 1033) have `tabindex` but no role and no selected
    state.
  - Errors and confirmations use `alert()` and `prompt()` (lines 1220–1481), including the typed
    DELETE and PURGE confirmations.
- **Action:**
  - Add a text label next to each colored score, such as "strong", "borderline", or "weak", or add
    an icon with `aria-label`.
  - Set `aria-current="page"` on the active nav button.
  - Give selectable rows `aria-selected`, or move the click target into a real `<button>` in the
    first cell.
  - Replace `alert()` with an `aria-live="assertive"` error region, and replace `prompt()` with a
    `<dialog>` that has a labeled input.
- **Done when:** You can navigate every primary flow by keyboard, and no state is conveyed only by
  color. Verify with an axe or Lighthouse accessibility run and record the result.

### T-35 — Extract frontend script and remove inline handlers (P3)

- **Standard:** Security (enables a strict CSP; see T-7); idiomatic code; linting.
- **Where:** [job_search/web/static/index.html](job_search/web/static/index.html), which is 1,620
  lines with inline `<script>` and `onclick`/`onchange` attributes throughout
- **Problem:**
  - The inline script and handlers force `script-src 'unsafe-inline'`.
  - Pipeline names and status options are put into `innerHTML` without escaping (lines 694–697,
    1019, and 1069). They are server constants today, but this becomes unsafe after T-23 makes
    pipelines user-configurable.
  - `api()` calls `response.json()` on non-JSON error responses and throws a misleading error.
- **Action:**
  - Move the script to `static/app.js` and the styles to `static/app.css`.
  - Replace inline handlers with `addEventListener` and event delegation.
  - Escape every interpolated value.
  - In `api()`, check the `content-type` before parsing.
  - Then remove `'unsafe-inline'` from `script-src` in T-7.
- **Done when:** `index.html` has no `on*=` attributes and the CSP has no `unsafe-inline` for
  scripts.

## Tests

### T-36 — Add assertion messages to bare test asserts (P2)

- **Standard:** Tests must include clear assertion failure messages where the framework supports
  them.
- **Where:** [tests/](tests/). Of 262 `assert` statements, 171 have no message:

  | File | Bare asserts |
  | --- | --- |
  | `test_api_jobs.py` | 51 |
  | `test_packets_and_tasks.py` | 36 |
  | `test_search.py` | 25 |
  | `test_cli_and_config.py` | 22 |
  | `test_adapters.py` | 19 |
  | `test_domain_rules.py` | 18 |

- **Action:** Add a message to each bare assert that states the expected behavior, for example
  `assert resp.status_code == 409, f"duplicate URL should conflict, got {resp.status_code}: {resp.json}"`.
  Include the actual response body for HTTP assertions.
- **Done when:** `grep -nE "^\s*assert [^,]+$" tests/*.py` returns no matches.

### T-37 — Cover untested boundary and failure paths (P3)

- **Standard:** Tests for boundary behavior and validation or error handling.
- **Where:** Gaps from the coverage report at `6aab6ba`:
  - `job_search/__main__.py`: 0%.
  - `domain/scheduler.py`: 77%. `_loop`, `start`, and `stop` are untested.
  - `web/validation.py`: 83%. Untested: non-string text, too-long text, boolean coercion, and
    integer `nullable` and `default` branches.
  - `domain/jobs.py` lines 25–46: the rejection branches of `validate_posting_url` (localhost, IPv6
    literal, private IPv4, and shorthand `127.1`) are untested, even though they are an SSRF control.
  - `domain/packets.py` lines 146–154: the invalid-JSON packet response.
  - `web/app.py` line 109: the non-API `HTTPException` path.
- **Action:** Add focused tests for each gap. Mark `__main__.py` with `# pragma: no cover` and a
  reason, or test it through `runpy`.
- **Done when:** Each listed branch is covered and total coverage stays at or above 80%.

## Documentation

### T-38 — Document the HTTP API and fix README inaccuracies (P3)

- **Standard:** Document public APIs, commands, and configuration flags; include troubleshooting.
- **Where:** [README.md](README.md)
- **Problem:**
  - The README has no reference for the roughly 30 HTTP endpoints.
  - "Current Scope" says searches run "on demand or daily", but the scheduler is disabled.
  - The status-code list will change with T-18.
  - The new configuration from T-2, T-22, and T-23 and the data lifecycle from T-31 need
    documentation.
- **Action:**
  - Add an "HTTP API" section with a table of method, path, request body fields, success status,
    and error statuses.
  - Correct the scheduler statements.
  - Update the configuration table as each configuration task lands.
- **Done when:** Every route in `routes.py` appears in the README table.
