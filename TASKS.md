# Compliance Task Ledger

Last reviewed: 2026-09-26.

This ledger separates work verified as complete from work that remains open.
New work is assigned stable IDs (`T-1`, `T-2`, …) so it can be referenced
unambiguously in commits, reviews, and handoffs. A task may move to **Closed
tasks** only after every listed closure criterion has direct test or inspection
evidence.

## Closed tasks

- [x] **Outbound-request SSRF protection.** All outbound requests use a
  destination-validation and pinned-address transport; redirects are validated
  hop-by-hop, approved board hosts are allowlisted, and tests cover private DNS
  answers, DNS rebinding, redirects, IPv4/IPv6, TLS hostname verification, and
  timeouts.

- [x] **External deployment protection.** Non-loopback binding requires bearer
  authentication, CSRF validation, TLS termination, and trusted proxy CIDRs.
  Tests verify local development, untrusted peers, authenticated proxy traffic,
  and invalid proxy configuration.

- [x] **Sensitive telemetry and capture redaction.** Normal logs/captures
  redact query credentials, headers, posting content, prompts, and model output;
  full capture is explicit opt-in with restrictive file permissions. Sentinel
  tests cover HTTP and Codex paths.

- [x] **API boundary validation.** JSON-object validation, strict booleans,
  bounded bulk IDs, and actionable client errors are applied to the affected
  API boundaries and covered by deterministic tests.

- [x] **Typed runtime configuration and controlled startup.** Runtime bounds
  are validated, and invalid configuration or unsafe external binding exits
  with one stable stderr error record and no traceback.

- [x] **Durable worker and scheduler primitives.** Web startup does not launch
  worker/scheduler threads; tasks persist in SQLite, use exclusive leases with
  heartbeat renewal, and have separate worker/scheduler entry points. Tests
  cover duplicate claims, lease expiry, renewal, and recovery.

- [x] **Repository-wide quality gate and CI.** The documented `./quality.sh`
  runs Ruff, unit tests, and branch coverage across production modules with an
  80% minimum; the version-controlled GitHub Actions workflow runs the same
  command for pushes and pull requests.

- [x] **Operational documentation.** README documents data lifecycle,
  retention, configuration, secure exposure, quality checks, and managed worker
  and scheduler commands. Documentation tests prevent reintroducing obsolete
  in-web-dispatcher guidance.

## Open tasks

### P0

- [ ] **T-1 — Complete real dependency injection at the presentation boundary.**
  `job_search/presentation/legacy.py` is still a 2,460-line composition and
  workflow module. It directly constructs `INFRASTRUCTURE = infrastructure()`,
  instantiates repositories/adapters through that bundle, opens database
  sessions through `connect`, invokes the Codex gateway (`legacy.py:1475`), and
  owns workflow adapters for packets, scoring, discovery, search, and task
  execution. Hiding concrete classes behind a composition bundle does not make
  these presentation responsibilities.

  **Closure criteria:**

  - The application factory/composition root constructs every concrete adapter
    and injects complete application services into route registration; no
    presentation module calls `infrastructure()`, references an infrastructure
    bundle, opens a database session, or instantiates a repository/gateway.
  - Presentation modules contain only HTTP request validation/mapping,
    response rendering, authentication/CSRF hooks, and top-level error mapping.
    Move remaining packet, scoring, discovery/search, capture, parsing, task,
    and startup workflows into application services; keep their concrete
    filesystem/HTTP/Codex/SQLite implementations in data access.
  - `job_search.worker` and `job_search.scheduler` invoke application services
    assembled by composition, rather than importing callbacks from the
    presentation package.
  - Split or remove `presentation/legacy.py` so route registration is readable
    and narrowly scoped; do not retain compatibility wrappers that perform
    workflow or data-access work.
  - Add AST-based tests that enforce these rules for every presentation module,
    and integration tests showing the injected services preserve current API,
    worker, and scheduler behavior.

### P1

- [ ] **T-2 — Make every handled exception observable through an injected
  telemetry port.** Several new or retained recovery paths still catch errors
  without the required unique event/log record: malformed JSON-LD is silently
  skipped in `data_access/job_posting_parser.py:106`; worker processor failures
  are converted into task messages in `worker.py:41` without telemetry; and
  heartbeat-renewal failures in the worker thread are unhandled. These violate
  the repository’s requirement that every caught exception identify its failure
  point with a stable code, component, operation, relevant identifier, and
  sanitized cause.

  **Closure criteria:**

  - Introduce a framework-independent observability port used by application
    services, workers, parsers, and data-access adapters; composition supplies
    the structured logger implementation.
  - Each intentional recovery/catch emits exactly one documented stable event
    containing error code, component, operation, relevant record/task/request
    ID where available, and a sanitized cause. A heartbeat failure must stop or
    safely abandon ownership rather than leave an unobserved daemon-thread
    exception.
  - Add a static regression test that inventories `except` blocks and requires
    either event emission or explicit rethrow to a documented top-level handler;
    allow narrowly scoped parsing-probe exceptions only with an inline recovery
    rationale and event.
  - Add deterministic tests for invalid JSON-LD, worker processor failure, and
    heartbeat renewal failure that assert durable state plus the emitted event.
