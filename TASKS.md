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

  Milestones:

  - [x] **T-1.1 — Extract presentation-owned configuration, logging, capture,
    HTTP, parsing, and filesystem adapters.**
  - [x] **T-1.2 — Assemble every application service in `composition.py` and
    remove presentation-layer infrastructure construction and session access.**
  - [x] **T-1.3 — Extract search, discovery, scoring, packet,
    initialization, and durable-task workflow adapters from `legacy.py`.**
  - [ ] **T-1.4 — Reduce presentation modules to HTTP mapping, validation,
    response rendering, security hooks, and top-level error handling.**
  - [x] **T-1.5 — Rewire the worker and scheduler through composition-owned
    application services rather than presentation callbacks.**
  - [ ] **T-1.6 — Add boundary and integration evidence for every T-1 closure
    criterion, run the quality gate, and close T-1.**

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

## Execution plan for T-1 and T-2

Complete the milestones in this dependency order: T-1.1, T-1.3, T-1.2, T-1.5,
T-1.4, T-1.6, then T-2. Within a milestone, work through its unchecked steps
in order. Each step names the expected change and the evidence needed to check
it off. Keep the existing T-1 and T-2
closure criteria authoritative: a passing test alone does not close a milestone
if the source still violates its boundary. Commit `03ca591` was an explicitly
requested intermediate T-1 checkpoint; inspect current Git state before
editing. Preserve unrelated changes to
`AGENTS.md`. For each completed T-1 milestone, check its milestone above and
commit that milestone's code, tests, and checkbox together with a descriptive
`AI Generated: ` message. Do not check T-1 itself until T-1.6 passes. Complete
and commit T-2 separately after its own closure audit.

Keep each edit to one adapter, workflow, or route family and update its direct
tests in the same slice. Run the relevant focused tests after a slice; run the
full `./quality.sh` at milestone boundaries or when a change affects shared
startup, security, or dependency wiring. Convert tests that import or patch
`legacy.py` as their corresponding behavior moves; do not postpone a mass test
rewrite until deletion. If a step exposes unexpected coupling, add a smaller
unchecked step here with a concrete exit check instead of adding another
presentation wrapper. Define the framework-independent telemetry port during
T-1.1 and use it in extracted workflows so T-2 does not have to rewire them.

### T-1.1 — Concrete runtime and data-access adapters

- [x] **1.1a — Reconcile the checkpoint.** Review `git status`, the T-1 diff,
  and `./quality.sh`; keep the user-owned `AGENTS.md` edit out of commits. Record
  any failing gate as the first repair target.
- [x] **1.1b — Extract runtime configuration.** Move path discovery, dotenv
  loading, CLI availability, and environment-backed settings out of
  `presentation/legacy.py`. Supply a typed configuration object and an injected
  environment mapping from composition. Test valid/invalid values and `.env`
  persistence with a temporary file; do not mutate real process environment
  variables in unit tests.
- [x] **1.1c — Compose logging and captures.** Build `StructuredTelemetry`,
  `CaptureStore`, log handlers, and their correlation-ID provider in
  `composition.py`; define the framework-independent telemetry port here and
  inject the resulting ports. Remove their construction and data-access
  re-exports from presentation. Preserve existing event codes as workflows
  move. Test redaction, capture replay, corrupt capture recovery, permissions,
  and correlation propagation.
- [x] **1.1d — Compose outbound clients and parsers.** Have composition build
  `SafeHttpClient`, `CapturingHttpGateway`, `JobBoardClient`, and posting/board
  parsers with explicit dependencies. Remove presentation-owned HTTP headers,
  gateway/parser construction, and fetch compatibility wrappers. Verify fake
  HTTP responses, rejected URLs, capture behavior, and board parsing.
- [x] **1.1e — Finish filesystem ownership.** Route source-document reads,
  packet path validation/catalog reads, atomic packet publication, and startup
  directory creation through data-access adapters. Test traversal rejection,
  absent optional files, publish collision, and failed publish cleanup.
- [x] **1.1f — Audit and commit.** Inspect every presentation module for direct
  environment, transport, parser, logging, capture, or filesystem adapter work;
  account for anything intentionally deferred to T-1.2 or T-1.3. Run
  `./quality.sh` (including at least 80% coverage), then check T-1.1 and commit.

### T-1.3 — Move workflows to application services

- [x] **1.3a — Move startup orchestration.** Move database initialization,
  query seeding, and task recovery decisions from `legacy.py` into an
  application service; keep SQLite and directory creation in data access. Test
  fresh startup and recovery using a temporary database.
- [x] **1.3b — Move durable-task execution.** Move bulk task progress, skipped
  item handling, and failure decisions into the application task service. Test
  one successful, skipped, and failed item with fake scoring/packet ports.
- [x] **1.3c — Move search runs.** Replace `_SearchRunAdapter` with explicit
  application ports for query selection, board calls, persistence, and run
  status. Test deduplication, filters, and failed board calls with fakes.
- [x] **1.3d — Move discovery.** Replace `_DiscoveryAdapter` with explicit
  ports for refinement, classification, level assessment, and persistence.
  Test discovered-job creation and failed model output with fakes.
- [x] **1.3e — Move scoring.** Move Codex scoring orchestration, score
  normalization/storage, and filter refresh decisions into application code.
  Inject the Codex adapter and test success and failure without the CLI.
- [x] **1.3f — Move packet generation.** Move packet context/prompt rules,
  validation, attribution retry, and publish decisions into application code.
  Inject Codex and packet-storage adapters; test attribution and publication
  failures without the CLI or Pandoc.
- [x] **1.3g — Verify and commit.** Inspect `legacy.py` for the listed workflow
  decisions and compatibility wrappers, run focused application tests plus
  `./quality.sh`, check T-1.3, and commit.

### T-1.2 — Complete composition and dependency injection

- [x] **1.2a — Define the injected contract.** Replace the permissive service
  dictionary/fallback pattern with a complete, explicit dependency contract
  for web routes and separate worker/scheduler process builders. A missing
  required service should fail at construction, not during a request.
  - [x] **1.2a.1 — Require the existing web services.** Replace the permissive
    dictionary and fallback lookup with named required fields, fail app
    construction when dependencies are absent, and migrate direct web tests
    to the injected factory. Run the quality gate.
  - [x] **1.2a.2 — Complete the web contract.** Add all remaining route-facing
    workflows and configuration/telemetry ports to the named contract; remove
    their presentation-side construction and test missing-field failures.
    - [x] **1.2a.2a — Inject scoring and packet workflows.** Compose the Codex
      scoring, on-demand scoring, and packet-generation services once per web
      application; replace presentation constructors and use fake ports in
      direct workflow tests.
    - [x] **1.2a.2b — Inject ingestion workflows.** Compose manual ingestion
      and rescraping with repository, board, filtering, clock, and telemetry
      ports; remove their presentation constructors.
    - [x] **1.2a.2c — Inject discovery and search workflows.** Compose the
      remaining model/board/persistence ports and move search-run construction
      out of presentation, with deterministic fake-boundary tests.
    - [x] **1.2a.2d — Inject runtime and observability ports.** Supply web
      configuration, security, correlation, logging, and capture dependencies
      through the named contract and verify missing-field failures.
  - [x] **1.2a.3 — Define process builders.** Give worker and scheduler separate
    composition builders keyed by the supplied database path, with focused
    tests that no web dependency bundle is required.
  - [x] **1.2a.4 — Audit and commit.** Verify no permissive dictionary or
    request-time service fallback remains, run the quality gate, then check
    1.2a.
- [x] **1.2b — Build all concrete services in composition.** Wire repositories,
  SQLite session providers, gateways, telemetry, clocks, policies, and the
  application services extracted in T-1.3. Replace generic
  `**operations`/`__dict__` ports with named, testable contracts. Keep
  application modules independent of Flask and concrete storage APIs.
- [x] **1.2c — Remove presentation construction.** Delete `INFRASTRUCTURE`,
  `OUTBOUND_HTTP_CLIENT`, `_DatabaseSessionProvider`/`connect`, and every
  presentation fallback that builds a repository, gateway, session, or service.
  Update direct-helper tests to construct application services with fakes.
  - [x] **1.2c.1 — Remove the obsolete infrastructure bundle and read wrappers.**
    Delete presentation-owned `INFRASTRUCTURE` and unused read-model helpers;
    keep the one direct persistence assertion at the read-model boundary.
  - [x] **1.2c.2 — Remove eager outbound and Codex gateways.** Rehome direct
    gateway tests at the adapter/composition boundary and eliminate the
    presentation globals and compatibility wrapper.
  - [x] **1.2c.3 — Remove session and service construction.** Replace the
    presentation session provider and any remaining construction/fallbacks;
    migrate direct-helper tests and verify the entire presentation package.
- [x] **1.2d — Verify and commit.** AST-inspect all presentation modules for
  infrastructure/session construction and verify injected API behavior with a
  temporary database. Run `./quality.sh`, check T-1.2, and commit.

### T-1.5 — Managed process wiring

- [x] **1.5a — Rewire the worker.** Build a task processor from composition
  using the CLI's `--database` path; make `worker.py` call the application
  processor without importing presentation. Preserve claim, heartbeat, and
  durable completion semantics.
- [x] **1.5b — Rewire the scheduler.** Build a search runner from composition
  using the CLI's `--database` path; make `scheduler.py` invoke the application
  runner after lease acquisition without importing presentation.
- [x] **1.5c — Verify and commit.** Test both CLI entry points with a temporary
  database and fake external adapters, including failure and lease paths. Run
  `./quality.sh`, check T-1.5, and commit.

### T-1.4 — Narrow the HTTP layer

- [x] **1.4a — Move console and read routes.** Register a small module for
  console, state, and read APIs; migrate its tests to the injected app factory.
- [x] **1.4b — Move jobs, companies, and configuration routes.** Register
  these route families with request validation and response mapping only;
  migrate their tests to the injected app factory.
  - [x] **1.4b.1 — Extract company-interest routes.** Move normalization and
    timestamp decisions to the company service; verify create/update through
    the injected app and commit.
  - [x] **1.4b.2 — Extract job routes.** Move job mutation, scoring, and scrape
    endpoints to an injected route module; verify success and validation paths
    and commit.
    - [x] **1.4b.2a — Move user-score orchestration.** Compute the default total,
      persist the scorecard, and refresh filtering in an injected application
      service; cover success and missing-job paths, then commit.
    - [x] **1.4b.2b — Move job endpoints.** Register job mutation, scoring, and
      scrape routes from a narrow module, preserving validation and API
      responses; verify with the injected app, then check 1.4b.2 and commit.
  - [x] **1.4b.3 — Extract configuration routes.** Move settings, runtime
    configuration, and confirmed purge endpoints; verify safe validation and
    persistence through the injected app, then check 1.4b and commit.
- [ ] **1.4c — Move search, packets, and tasks routes.** Register these route
  families with rendering and mapping only; migrate their tests to the injected
  app factory.
- [ ] **1.4d — Remove `presentation/legacy.py`.** Move any remaining pure
  domain rules to application and I/O to data access; delete workflow and
  data-access compatibility wrappers. Update app factory and remaining tests
  after confirming no process or test imports legacy.
- [ ] **1.4e — Verify and commit.** Exercise representative success, validation,
  authentication, and unexpected-error HTTP responses; run `./quality.sh`,
  check T-1.4, and commit.

### T-1.6 — Boundary proof and T-1 closure

- [ ] **1.6a — Strengthen AST tests.** Scan every presentation module for
  direct data-access imports, adapter/repository/session construction, SQL,
  filesystem/transport operations, and application workflow decisions. Assert
  worker/scheduler do not import presentation and application services do not
  depend on Flask or concrete infrastructure.
- [ ] **1.6b — Run integration and quality evidence.** Verify injected API,
  worker, and scheduler flows with temporary storage and fake HTTP/Codex/Pandoc
  boundaries. Run `./quality.sh`, confirm at least 80% branch coverage, and
  inspect the source against every T-1 closure criterion.
- [ ] **1.6c — Close T-1.** Check T-1.6 and T-1 only when all five closure
  criteria above have direct evidence; commit the final T-1 proof and checkboxes.

### T-2 — Observable exception handling

- [ ] **2a — Inventory catches and define events.** Enumerate every `except`
  in `job_search/`; classify rethrow versus recovery. Document a stable unique
  error code, component, operation, sanitized cause, identifiers, and valid
  recovery behavior for each handled exception.
- [ ] **2b — Extend the injected telemetry port.** Reuse the port introduced
  in 1.1c; thread it through all remaining application services, parsers,
  worker, and scheduler. Keep its structured logger implementation in
  data access/composition, without importing Flask or leaking secrets.
- [ ] **2c — Repair known failures.** Emit one event for malformed JSON-LD in
  `job_posting_parser.py` and worker processor failures. Handle heartbeat
  renewal failure so ownership is stopped or safely abandoned, with a durable
  outcome and one event. Check all other inventoried catches as well.
- [ ] **2d — Prove the failure paths.** Add deterministic tests for malformed
  JSON-LD, processor failure, and heartbeat failure that assert both persisted
  state and event fields. Add an AST inventory test keyed by module/function
  that fails on unclassified new catches and checks each known catch for event
  emission or explicit rethrow to a documented top-level handler; document
  narrowly scoped parsing recovery inline.
- [ ] **2e — Close T-2.** Run `./quality.sh`, confirm at least 80% branch
  coverage, audit every T-2 closure criterion, check T-2, and commit its code,
  tests, and checkbox separately from T-1.
