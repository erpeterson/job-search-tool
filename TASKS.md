# Compliance backlog

This backlog records gaps found in the repository against `AGENTS.md` on
2026-09-24.  It is intentionally limited to changes needed for compliance,
not general product enhancements.  `./.venv/bin/python -m unittest discover -v`
passed (15 tests), but the repository has no configured coverage or linting
gate, so that result is not evidence of the required 80% coverage.

## P0 — security and safe operation

- [x] **Prevent SSRF in all outbound job-posting requests.** `http_url()` in
  `job_search/validation.py` rejects literal private IP addresses, but accepts
  hostnames without resolving them; `fetch_url()` in `app.py` then follows
  redirects by default.  An attacker who can reach the API can use a hostname
  resolving to a private address, DNS rebinding, or a redirect to access local
  services.  Create an outbound HTTP client/adapter that resolves and checks
  every destination (including each redirect) against non-global addresses,
  pins the approved address for the request where feasible, disables or
  manually validates redirects, and applies allowlists for supported job
  boards.  Route `scrape_job_from_url`, LinkedIn, and Indeed through it.  Add
  deterministic tests for private DNS answers, redirects to private IPs, and
  normal public URLs.

- [x] **Require authentication and CSRF protection before allowing non-loopback
  binding.** `JOB_SEARCH_HOST` can expose the unauthenticated Flask app beyond
  `127.0.0.1`; its endpoints can write `.env`, invoke Codex, purge jobs, and
  delete records.  Enforce loopback-only binding unless explicit production
  authentication, authorization, TLS/reverse-proxy trust configuration, and
  CSRF protection are configured.  Reject unsafe startup configuration with a
  clear error.  Cover unauthenticated state-changing requests, CSRF rejection,
  and permitted local development behavior in tests.

- [x] **Redact sensitive data before logs and replay captures are written.**
  `fetch_url`, `log_api_call`, and `write_capture` persist complete URLs,
  headers, response bodies, Codex prompts, and model output under `logs/` and
  `captures/`.  Query tokens and personal/job-application content can therefore
  be retained in plaintext.  Introduce a centralized redaction/classification
  utility; remove URL credentials/query secrets, authorization/cookie headers,
  and sensitive response/prompt fields before telemetry.  Make full replay
  capture opt-in with documented retention and restrictive permissions; test
  that known secret values never appear in logs or capture files.

## P1 — architecture, validation, and resilience

- [ ] **Split `app.py` into the required three tiers.** The 4,976-line module
  currently combines Flask routes and embedded HTML/JS (presentation), search,
  scoring, packet, filtering, scheduler workflows (business logic), and SQLite,
  filesystem, HTTP, subprocess, and capture operations (data access).
  Introduce modules/packages for presentation routes/templates, application
  services/use cases, domain models/validation, and repositories/adapters.
  Inject dependencies through an application factory so business logic has no
  Flask, SQLite, Requests, or subprocess dependency.  Preserve public API
  behavior and add focused unit tests for services plus route/integration tests.

- [x] **Validate all request bodies and field types consistently at API
  boundaries.** Several routes bypass `require_json_object()` (for example
  bulk score/generate at `app.py:2622`/`:2637`, packet attach at `:2668`,
  rescrape at `:2812`, and delete at `:2856`).  A JSON array causes an unhandled
  `AttributeError` when `.get()` is called.  Replace ad-hoc `bool(...)`
  coercion for `force_refresh` and `enabled` with a validator accepting only
  JSON booleans, require the intended fields, impose list-size limits for bulk
  job IDs, and return actionable 400 responses.  Add boundary tests for arrays,
  scalars, string booleans, nulls, duplicate/oversized ID lists, and each
  affected endpoint.

- [x] **Centralize exception translation and make every recovery observable.**
  The project requires each caught exception to emit a unique telemetry event
  and a structured log with stable error code, component, operation,
  identifiers, and sanitized cause.  Current silent recovery includes malformed
  stored JSON/captures (`app.py:620`, `:824`), invalid JSON-LD (`:1601`), score
  coercion (`:1703`), and request-handler `ValueError` branches (for example
  `:2631`, `:2644`, `:2678`).  The `HTTPException` branch in `api_error` also
  returns without telemetry.  Define typed application exceptions and one
  presentation error mapper; log a distinct event for each recovery (including
  capture corruption) without leaking internals to clients.  Add tests that
  assert status, safe response, event code, and correlation/run ID.

- [x] **Validate environment configuration before startup and fail safely.**
  Integer conversion of `CODEX_CLI_TIMEOUT_SECONDS`, `JOB_SEARCH_PORT`, log
  sizes/counts, and intervals occurs at import time in `app.py:53-60`; malformed
  external environment values crash without a controlled error or telemetry.
  Move configuration into a typed settings loader with bounds (positive timeout,
  valid port, non-negative rotation limits, safe host policy), a stable startup
  error code, and documented defaults.  Use the same validation when the config
  endpoint updates `.env`; add tests for invalid and boundary values.

- [x] **Replace in-process daemon threads and mutable global task state with a
  durable worker abstraction.** `BACKGROUND_TASKS` and `threading.Thread` lose
  task status on restart and can run long external work inside the web process;
  the scheduler has the same issue.  Define a task repository and worker/queue
  interface, persist task state and correlation IDs, enforce concurrency/time
  limits, support recovery of interrupted tasks, and make scheduling a separate
  managed process or explicitly documented single-process development feature.
  Test restart recovery and worker failure paths.

## P2 — quality gates, tests, and documentation

- [x] **Add reproducible quality gates with an enforced 80% coverage minimum.**
  `requirements.txt` only lists runtime dependencies, and no lint, static
  analysis, coverage configuration, or CI workflow exists.  Add pinned dev
  tooling (for example Ruff and coverage.py), configuration, and a single
  documented command/CI job that runs formatting/lint checks, static analysis,
  tests, and branch coverage with `--fail-under=80`.  Keep generated data,
  virtual environments, logs, and captures out of measurement.  Update
  `README.md` with setup and troubleshooting for the quality command.

- [x] **Expand tests to cover required business, error, and integration paths.**
  The current 15 tests focus on small validation and level-equivalency cases;
  they do not cover database repositories/migrations, search filtering,
  configuration persistence, packet filesystem safety, error mapping, scheduler
  behavior, or HTTP adapter failures.  Add deterministic tests using temporary
  databases/directories and fake HTTP/Codex/Pandoc adapters.  For every business
  service, include normal behavior, boundary validation, and at least one
  logged failure/recovery assertion, then meet the new coverage threshold.

- [x] **Document supported operations and safe data lifecycle.** The README
  describes running and many features, but does not define data ownership,
  capture/log retention and deletion, backup/restore, production exposure
  requirements, configuration validation, or quality checks.  Add concise
  sections for configuration, data locations/permissions/retention, safe purge
  and recovery behavior, operational logs/correlation IDs, security deployment
  constraints, and run/test/lint/troubleshooting commands.
