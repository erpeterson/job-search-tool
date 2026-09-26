# Agent Guidance

This guidance applies to agents working on the job-search-tool codebase. It does not apply to application packets or source material.

## Operating Principles

- Keep work inspectable. Prefer plain files, clear diffs, deterministic scripts, structured logs, and explicit assumptions.
- Optimize for reproducibility before convenience. Pin dependencies where practical and record deviations.
- Surface uncertainty early. If a requirement, input, or instruction is ambiguous, consult a human to resolve the ambiguity.

## Autonomous Task Completion

When assigned an implementation task, continue working until all stated
acceptance criteria are met and verified.

Do not stop after partial progress, a passing intermediate test, a quality-gate checkpoint, or a turn boundary. Treat these as evidence to select and execute the next unmet requirement.

Only return control to the user when:
- a decision or approval is required;
- an external blocker cannot be resolved within the authorized scope; or
- the entire task is complete, verified, documented as required, and committed when requested.

Maintain an explicit acceptance-criteria checklist. Do not mark a task complete or commit it until every criterion has direct evidence.

## Architecture Standards

- Follow SOLID design principles unless the project definition explicitly requires a different architectural style.
- Use a 3-tier architecture for application code:
  - Presentation: user interface, CLI, API handlers, controllers, request/response mapping, and other delivery mechanisms.
  - Business logic: domain rules, use cases, workflows, validation, orchestration, and application decisions.
  - Data access: persistence, external service clients, repositories, data mappers, and storage-specific concerns.
- Keep presentation, business logic, and data access concerns separated by clear module boundaries.
- Do not let presentation code contain domain rules or persistence details.
- Do not let data access code make user-interface or workflow decisions.
- Keep business logic independent of framework-specific presentation and storage APIs where practical.
- Use well-defined design patterns where they clarify responsibilities or reduce coupling, such as MVC, factories, strategy, adapter, repository, chain of responsibility, observer, or singleton.
- Apply patterns intentionally. Do not add pattern ceremony when straightforward code is clearer, and avoid singleton use for mutable global state unless there is a strong reason.
- For very small tasks, preserve the separation logically even if the implementation uses fewer files.
- Web applications must include top-level exception handling that transforms uncaught exceptions into appropriate HTTP response codes.
- Web exception handlers should avoid leaking sensitive implementation details in responses while preserving diagnostic details in logs and telemetry.
- Command-line applications must include top-level exception handling that returns OS-appropriate process result codes for success and failure.
- Command-line applications must write `ERROR` severity log messages to stderr.
- Command-line applications must write non-error log severities to stdout only when the caller provides a verbose flag such as `-v` or `--verbose`.
- Command-line applications should keep normal command output separate from diagnostic logging so scripts can consume stdout predictably.

## Telemetry And Evidence

- Do not delete, rewrite, or hide logs, telemetry, transcripts, model-call records, tool-call records, or evaluation outputs.
- Every caught exception must emit a unique telemetry event or metric that identifies the failure point, sometimes called a blame metric.
- Every caught exception must also write a unique log line with enough context for a human or ops agent to determine the likely root cause.
- Exception telemetry and logs should include a stable error code, component/module, operation, relevant identifiers, and sanitized cause details.
- Do not swallow exceptions silently. If an exception is intentionally handled, record why the recovery path is valid.

## Code And Dependency Standards

- Prefer the smallest implementation that satisfies the project definition and acceptance criteria.
- Avoid unrelated refactors.
- Write idiomatic code for the language(s) being used.
- Use language-appropriate linters and static-analysis tools, such as PMD or Checkstyle for Java and PEP 8-oriented tooling for Python.
- Adhere to the selected linting/static-analysis guidelines unless the project definition or global standards explicitly override them.
- Document any linter rule suppressions or deviations with a specific reason.
- Do not add dependencies unless they materially reduce risk or complexity.
- Prefer open-source dependencies that can be installed inside generated Docker containers.
- Pin dependency versions where practical for replayability.
- Never bake secrets into source files, generated images, logs, or committed artifacts.

## Cross-Cutting Application Standards

- Configuration: keep environment-specific values in configuration or environment variables, not hardcoded constants. Provide safe defaults where practical and never require secrets in source-controlled files.
- Input validation: validate all external inputs at system boundaries, including CLI arguments, HTTP requests, files, environment variables, and imported data. Return actionable validation errors.
- Security: design against the OWASP Top 10 by default. Avoid injection, broken access control, authentication/session mistakes, insecure design, security misconfiguration, vulnerable dependencies, identification/authentication failures, integrity failures, logging/monitoring gaps, SSRF, path traversal, unsafe deserialization, and secret leakage.
- Tests: require at least 80% code coverage unless the project definition explicitly sets a different threshold. Tests must be human-readable and include clear assertion failure messages where the test framework supports them.
- Test scope: include tests for business logic, boundary behavior, validation/error handling, and at least one failure path. Prefer deterministic tests that run without external services unless explicitly required.
- Observability: use structured logs where practical and carry correlation, request, or run IDs across logs and telemetry. Emit start, success, and failure events for major operations.
- Accessibility: web and UI projects should use semantic markup where applicable, support keyboard navigation for primary flows, label form controls, and avoid color-only status indicators.
- Data handling: define ownership and lifecycle for generated data. Sanitize user-provided filenames and paths. Avoid irreversible destructive operations without explicit confirmation.
- Performance: avoid obvious unbounded loops, unbounded memory growth, N+1 data access patterns, and blocking request handlers on long-running work unless intentionally designed.
- Documentation: generated projects should include minimal run, test, configuration, and troubleshooting instructions. Public APIs, commands, and configuration flags should be documented.

## Git And File Safety

- Do not revert or overwrite user changes unless explicitly instructed.
- Stage and commit only files related to the requested work.
- Do not use destructive Git commands to simplify cleanup.
