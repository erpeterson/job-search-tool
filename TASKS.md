# Compliance Task Ledger

Last reviewed: 2026-09-29.

This ledger separates work verified as complete from work that remains open.
Task IDs are stable references for commits, reviews, and handoffs. A task moves
to **Closed tasks** only when every closure criterion has direct inspection and
test evidence.

## Closed tasks

- [x] **Outbound-request SSRF protection.** Outbound requests use destination
  validation and pinned-address transport; redirects are validated hop-by-hop,
  approved board hosts are allowlisted, and tests cover private DNS answers,
  DNS rebinding, redirects, IPv4/IPv6, TLS hostname verification, and timeouts.

- [x] **External deployment protection.** Non-loopback binding requires bearer
  authentication, CSRF validation, TLS termination, and trusted proxy CIDRs.

- [x] **Sensitive telemetry and capture redaction.** Normal logs/captures
  redact credentials, headers, posting content, prompts, and model output;
  full capture is explicit opt-in with restrictive file permissions.

- [x] **API boundary validation.** JSON-object validation, strict booleans,
  bounded bulk IDs, and actionable client errors apply at API boundaries.

- [x] **Typed runtime configuration and controlled startup.** Invalid runtime
  configuration or unsafe external binding emits one stable stderr record and
  exits without a traceback.

- [x] **Durable worker and scheduler primitives.** Web startup does not launch
  worker/scheduler threads; durable tasks use exclusive, renewable SQLite
  leases and separate worker/scheduler entry points.

- [x] **Repository-wide quality gate and CI.** `./quality.sh` runs Ruff, tests,
  and production branch coverage with an 80% minimum; GitHub Actions runs the
  same command on pushes and pull requests.

- [x] **Operational documentation.** README documents data lifecycle,
  retention, configuration, secure exposure, quality checks, and managed
  worker/scheduler commands.

- [x] **T-1 — Real dependency injection at the presentation boundary.**
  `presentation/legacy.py` has been removed. The composition root assembles
  concrete adapters and injects named application services; presentation
  modules are limited to HTTP mapping/rendering/security/error handling; and
  worker/scheduler composition does not import presentation. AST boundary and
  injected API/worker/scheduler integration tests enforce this separation.

- [x] **T-2 — Observable handled exceptions.** A framework-independent
  telemetry port records recovery and failure events. `EXCEPTION_INVENTORY.md`
  and its AST regression test classify every current `except` as an event,
  translation/rethrow, or terminal boundary. Targeted tests cover malformed
  JSON-LD, worker processor failure, and heartbeat-renewal failure.

## Open tasks

No open compliance tasks as of this review.
