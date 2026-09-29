# Exception inventory for T-2

This inventories every current `except` in `job_search/` plus the web entry
point. The static regression test in 2d will key on module and enclosing
function, not line numbers. `Recover` means execution continues after the
catch and requires one event at that point. `Rethrow` means the exception is
propagated to the named outer boundary, which owns the one failure event.
`Translate` changes exception type but still propagates to that boundary.

Every failure event must carry its listed stable error code, component,
operation, a relevant job/task/query/request/service ID when available, and a
sanitized cause (at minimum the exception class). The injected structured
telemetry implementation redacts field values and writes one JSON log line per
event. The web entry point and CLI fatal handlers use a structured stderr
record only when composition fails before telemetry exists.

| Catch (module:function) | Disposition and event owner | Stable code / event | Valid recovery or destination |
| --- | --- | --- | --- |
| `app:module` | Terminal stderr | `STARTUP_CONFIGURATION_FAILED` / `startup_configuration_failed` | Exit 2; no telemetry graph exists yet. |
| `config:_integer` | Translate → `app:module` | `STARTUP_CONFIGURATION_FAILED` | Invalid config exits before startup. |
| `security:load_request_security` | Translate → `app:module` | `STARTUP_CONFIGURATION_FAILED` | Unsafe proxy CIDR exits before startup. |
| `validation:integer` | Translate → `presentation.routes:api_error` | `API_CLIENT_INPUT_INVALID` | Invalid request returns 400. |
| `application.job_scoring_policy:JobScoringPolicy.score` | Translate → API/worker boundary | `API_UNHANDLED_EXCEPTION` / `WORKER_PROCESSOR_FAILED` | Invalid model output fails scoring. |
| `application.bulk_task_service:run` (operation lookup) | Translate → process boundary | `WORKER_FATAL_FAILURE` | Unsupported operation cannot be dispatched. |
| `data_access.board_gateway:fetch` | Translate → search runner | `JOB_SEARCH_QUERY_FAILED` | Unsupported board is recorded against query. |
| `http_client:PinnedAddressTransport.get` | Translate → capturing HTTP gateway | `HTTP_OUTBOUND_FAILED` | Pinned connection failure is propagated. |
| `http_client:SafeHttpClient._validate_destination` (URL) | Translate → capturing HTTP gateway | `HTTP_OUTBOUND_FAILED` | Unsafe URL is rejected. |
| `http_client:SafeHttpClient._validate_destination` (DNS) | Translate → capturing HTTP gateway | `HTTP_OUTBOUND_FAILED` | DNS failure is rejected. |
| `http_client:SafeHttpClient._validate_destination` (IP) | Translate → capturing HTTP gateway | `HTTP_OUTBOUND_FAILED` | Invalid resolved IP is rejected. |
| `task_repository:TaskRepository._connection` | Rethrow → caller boundary | Caller-specific code | Roll back before propagating the original failure. |
| `data_access.http_gateway:CapturingHttpGateway.get` | Observe and rethrow | `HTTP_OUTBOUND_FAILED` / `http_outbound_failed` | Capture/log attempt; caller handles failure. |
| `data_access.codex_json_gateway:CodexJsonGateway.complete` | Observe and rethrow | `CODEX_CLI_CALL_FAILED` / `codex_cli_call_failed` | Capture/log failed invocation; caller handles failure. |
| `application.packet_draft_service:PacketDraftService.generate` | Observe and rethrow | `APPLICATION_PACKET_GENERATION_FAILED` / `application_packet_generation_failed` | Preserve attribution and propagate failure. |
| `validation:http_url` | Recover, parsing probe | `URL_IP_PROBE_FAILED` / `url_ip_probe_failed` | A syntactically valid DNS hostname is not an IP literal. |
| `security:is_loopback_host` | Recover, parsing probe | `SECURITY_HOST_IP_PROBE_FAILED` / `security_host_ip_probe_failed` | A hostname is not an IP literal; continue host policy. |
| `security:trusted_proxy_peer` | Recover, reject | `PROXY_ADDRESS_INVALID` / `proxy_address_invalid` | Malformed immediate peer is denied. |
| `data_access.model_output_parser:parse_model_json` | Recover or rethrow, parsing probe | `MODEL_OUTPUT_FENCE_RECOVERED` / `model_output_fence_recovered` | Embedded JSON object may be extracted; otherwise propagate decode failure. |
| `data_access.job_posting_parser:JobPostingParser._json_ld` | Recover, skip script | `JOB_POSTING_JSON_LD_INVALID` / `job_posting_json_ld_invalid` | Other JSON-LD blocks or HTML selectors remain usable. |
| `data_access.capture_store:CaptureStore.read` | Recover, cache miss | `CAPTURE_CORRUPTION_RECOVERED` / `capture_corruption_recovered` | Corrupt replay is ignored; live request remains available. |
| `application.discovery_service:DiscoveryService.refine_query` | Recover, preserve query | `QUERY_REFINEMENT_INVALID_JSON` / `query_refinement_invalid_json` | Invalid model output must not replace stored query. |
| `application.search_run_service:SearchRunService.run` (fetch) | Recover, next query | `JOB_SEARCH_QUERY_FAILED` / `job_search_query_failed` | One board failure must not abort the entire search run. |
| `application.search_run_service:SearchRunService.run` (refine) | Recover known Codex failure; otherwise rethrow | `QUERY_REFINEMENT_FAILED` / `query_refinement_failed` | Keep original query and continue the run. |
| `application.bulk_task_service:BulkTaskService.run` (item) | Recover, mark item error | Operation-specific `BULK_CODEX_SCORE_FAILED` or `BULK_APPLICATION_PACKET_FAILED` | Other durable items continue. |
| `application.manual_job_service:ManualJobService.create` (scrape) | Recover, fallback posting | `MANUAL_JOB_SCRAPE_FAILED` / `manual_job_scrape_failed` | User-supplied details remain usable. |
| `application.manual_job_service:ManualJobService.create` (score) | Recover, preserve job | `MANUAL_JOB_AUTO_SCORE_FAILED` / `manual_job_auto_score_failed` | Scraped job remains tracked without auto-score. |
| `application.job_service:JobService._present` | Recover, empty scorecard | `JOB_SCORECARD_PARSE_RECOVERED` / `job_scorecard_parse_recovered` | Corrupt optional scorecard must not hide the job. |
| `presentation.packet_routes:api_generate_application_packet` (missing) | Recover, HTTP 404 | `PACKET_GENERATION_JOB_MISSING` / `packet_generation_job_missing` | Requested job does not exist. |
| `presentation.packet_routes:api_generate_application_packet` (associated) | Recover, HTTP 409 | `PACKET_GENERATION_EXISTS` / `packet_generation_already_associated` | Preserve existing packet association. |
| `presentation.packet_routes:api_attach_application_packet` | Recover, HTTP 400 | `PACKET_ATTACHMENT_REJECTED` / `packet_attachment_rejected` | Invalid attachment path is refused. |
| `presentation.packet_routes:api_application_packet_content` | Recover, HTTP 404 | `PACKET_CONTENT_PATH_REJECTED` / `packet_content_path_rejected` | Missing/unsafe content is not served. |
| `presentation.packet_routes:api_application_packet_render` | Recover, HTTP 404 | `PACKET_RENDER_PATH_REJECTED` / `packet_render_path_rejected` | Missing/unsafe content is not rendered. |
| `worker:process_one` (processor) | Recover, durable item error | `WORKER_PROCESSOR_FAILED` / `worker_processor_failed` | Claim is completed as error and later work can continue. |
| `worker:process_one` (heartbeat; to add) | Recover, abandon ownership | `WORKER_HEARTBEAT_FAILED` / `worker_heartbeat_failed` | Stop processing/renewal and persist a safe error outcome without double completion. |
| `worker:main` | Terminal stderr + injected telemetry if available | `WORKER_FATAL_FAILURE` / `worker_fatal_failure` | Exit 1 after a process-level failure. |
| `scheduler:main` | Terminal stderr + injected telemetry if available | `SCHEDULER_FATAL_FAILURE` / `scheduler_fatal_failure` | Exit 1 after a process-level failure. |

Flask's `presentation.routes:api_error` is registered as a top-level exception
handler rather than an AST `except`; it emits `API_CLIENT_INPUT_INVALID`,
`API_HTTP_EXCEPTION`, or `API_UNHANDLED_EXCEPTION` before responding.
