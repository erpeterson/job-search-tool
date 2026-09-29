"""Keep each caught exception tied to an observable recovery or a known boundary."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Keyed by module and enclosing function rather than source line. Each entry is
# (caught type, disposition, evidence in the handler). Repeated catches in a
# function remain separate entries, so adding one requires an explicit review.
EXPECTED_CATCHES = {
    "app:module": (
        ("(StartupConfigurationError, StartupSecurityError)", "terminal", "startup_failure(exc, controlled=True)"),
        ("Exception", "terminal", "startup_failure(exc, controlled=False)"),
    ),
    "config:_integer": (("(TypeError, ValueError)", "translate", "StartupConfigurationError"),),
    "security:is_loopback_host": (("ValueError", "translate", "StartupSecurityError"),),
    "security:load_request_security": (("ValueError", "translate", "StartupSecurityError"),),
    "security:trusted_proxy_peer": (("ValueError", "event", "PROXY_ADDRESS_INVALID"),),
    "validation:http_url": (("ValueError", "translate", "RequestValidationError"),),
    "validation:integer": (("(TypeError, ValueError)", "translate", "RequestValidationError"),),
    "task_repository:TaskRepository._connection": (("Exception", "rethrow", "rollback"),),
    "http_client:PinnedAddressTransport.get": (("OSError", "translate", "OutboundRequestError"),),
    "http_client:SafeHttpClient._validate_destination": (
        ("RequestValidationError", "translate", "OutboundRequestError"),
        ("socket.gaierror", "translate", "OutboundRequestError"),
        ("ValueError", "translate", "OutboundRequestError"),
    ),
    "data_access.board_gateway:CallableBoardGateway.fetch": (("KeyError", "translate", "ValueError"),),
    "data_access.http_gateway:CapturingHttpGateway.get": (
        ("(requests.RequestException, OutboundRequestError)", "event", "HTTP_OUTBOUND_FAILED"),
    ),
    "data_access.codex_json_gateway:CodexJsonGateway.complete": (("Exception", "deferred", "error = exc"),),
    "data_access.capture_store:CaptureStore.read": (("json.JSONDecodeError", "event", "CAPTURE_CORRUPTION_RECOVERED"),),
    "data_access.job_posting_parser:JobPostingParser._json_ld": (
        ("json.JSONDecodeError", "event", "JOB_POSTING_JSON_LD_INVALID"),
    ),
    "data_access.model_output_parser:parse_model_json": (
        ("json.JSONDecodeError", "event", "MODEL_OUTPUT_FENCE_RECOVERED"),
    ),
    "application.packet_draft_service:PacketDraftService.generate": (("Exception", "deferred", "error = exc"),),
    "application.bulk_task_service:BulkTaskService.run": (
        ("KeyError", "translate", "ValueError"),
        ("Exception", "event", "self._telemetry.event"),
    ),
    "application.discovery_service:DiscoveryService.refine_query": (
        ("json.JSONDecodeError", "event", "QUERY_REFINEMENT_INVALID_JSON"),
    ),
    "application.job_scoring_policy:JobScoringPolicy.score": (("json.JSONDecodeError", "translate", "RuntimeError"),),
    "application.job_service:JobService._present": (
        ("json.JSONDecodeError", "event", "JOB_SCORECARD_PARSE_RECOVERED"),
    ),
    "application.manual_job_service:ManualJobService.create": (
        ("Exception", "event", "self._report_failure('scrape'"),
        ("Exception", "event", "self._report_failure('score'"),
    ),
    "application.search_run_service:SearchRunService.run": (
        ("Exception", "event", "JOB_SEARCH_QUERY_FAILED"),
        ("Exception", "event", "QUERY_REFINEMENT_FAILED"),
    ),
    "presentation.packet_routes:api_generate_application_packet": (
        ("PacketJobMissingError", "event", "PACKET_GENERATION_JOB_MISSING"),
        ("PacketAlreadyAssociatedError", "event", "PACKET_GENERATION_EXISTS"),
    ),
    "presentation.packet_routes:api_attach_application_packet": (
        ("ValueError", "event", "PACKET_ATTACHMENT_REJECTED"),
    ),
    "presentation.packet_routes:api_application_packet_content": (
        ("(ValueError, FileNotFoundError)", "event", "PACKET_CONTENT_PATH_REJECTED"),
    ),
    "presentation.packet_routes:api_application_packet_render": (
        ("(ValueError, FileNotFoundError)", "event", "PACKET_RENDER_PATH_REJECTED"),
    ),
    "presentation.cli:main": (
        ("Exception", "terminal", "startup_failure(exc, controlled=False"),
        ("Exception", "event", "WEB_FATAL_FAILURE"),
    ),
    "worker:process_one.heartbeat": (("Exception", "deferred", "heartbeat_failures.append(exc)"),),
    "worker:process_one": (("Exception", "event", "WORKER_PROCESSOR_FAILED"),),
    "worker:main": (("Exception", "terminal", "WORKER_FATAL_FAILURE"),),
    "worker:_main": (("Exception", "event", "WORKER_FATAL_FAILURE"),),
    "scheduler:main": (("Exception", "terminal", "SCHEDULER_FATAL_FAILURE"),),
    "scheduler:_main": (("Exception", "event", "SCHEDULER_FATAL_FAILURE"),),
}

DEFERRED_EVENT_CODES = {
    "data_access.codex_json_gateway:CodexJsonGateway.complete": "CODEX_CLI_CALL_FAILED",
    "application.packet_draft_service:PacketDraftService.generate": "APPLICATION_PACKET_GENERATION_FAILED",
    "worker:process_one.heartbeat": "WORKER_HEARTBEAT_FAILED",
}
OBSERVATION_CALLS = {"event", "log", "_observe", "observe", "_log_failure", "_report_failure"}


def _calls_observer(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) in OBSERVATION_CALLS
        for node in ast.walk(handler)
    )


class CatchCollector(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.scope: list[str] = []
        self.catches: dict[str, list[ast.ExceptHandler]] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.functions[f"{self.module}:{'.'.join(self.scope)}"] = node
        self.generic_visit(node)
        self.scope.pop()

    def visit_Try(self, node: ast.Try) -> None:
        if node.handlers:
            key = f"{self.module}:{'.'.join(self.scope) if self.scope else 'module'}"
            self.catches.setdefault(key, []).extend(node.handlers)
        self.generic_visit(node)


class ExceptionInventoryTests(unittest.TestCase):
    def test_every_catch_has_a_documented_observation_or_rethrow(self):
        actual: dict[str, list[ast.ExceptHandler]] = {}
        functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for path in (ROOT / "app.py", *(ROOT / "job_search").rglob("*.py")):
            module = (
                "app"
                if path.name == "app.py"
                else ".".join(path.relative_to(ROOT / "job_search").with_suffix("").parts)
            )
            collector = CatchCollector(module)
            collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            actual.update(collector.catches)
            functions.update(collector.functions)

        self.assertEqual(
            set(actual), set(EXPECTED_CATCHES), "Review any new or removed catch and update its disposition"
        )
        inventory = (ROOT / "EXCEPTION_INVENTORY.md").read_text(encoding="utf-8")
        for key, expectations in EXPECTED_CATCHES.items():
            with self.subTest(catch=key):
                handlers = actual[key]
                self.assertEqual(len(handlers), len(expectations), f"Review changed catch count in {key}")
                self.assertIn(f"`{key}`", inventory, f"Document the destination for {key}")
                for handler, (caught_type, disposition, marker) in zip(handlers, expectations, strict=True):
                    body = "\n".join(ast.unparse(statement) for statement in handler.body)
                    self.assertEqual(ast.unparse(handler.type), caught_type, f"Review changed caught type in {key}")
                    self.assertIn(marker, body, f"{key} must preserve its {disposition} evidence")
                    if disposition in {"translate", "rethrow"}:
                        self.assertTrue(
                            any(isinstance(node, ast.Raise) for node in ast.walk(handler)),
                            f"{key} must propagate to its documented top-level handler",
                        )
                    elif disposition == "event":
                        self.assertTrue(_calls_observer(handler), f"{key} must call its injected observer")
                    elif disposition == "terminal":
                        self.assertTrue(
                            any(
                                isinstance(node, ast.Call)
                                and isinstance(node.func, ast.Name)
                                and node.func.id in {"startup_failure", "print"}
                                for node in ast.walk(handler)
                            ),
                            f"{key} must write a terminal error record",
                        )
                    elif disposition == "deferred":
                        owner = "worker:process_one" if key.endswith(".heartbeat") else key
                        owner_source = ast.unparse(functions[owner])
                        self.assertIn(
                            DEFERRED_EVENT_CODES[key], owner_source, f"{key} must have a deferred failure event"
                        )
                        self.assertTrue(
                            any(isinstance(node, ast.Raise) for node in ast.walk(functions[owner])),
                            f"{key} must stop or rethrow after its deferred event",
                        )

    def test_parsing_recovery_has_inline_rationale(self):
        for relative_path, rationale in (
            ("data_access/model_output_parser.py", "A prose wrapper is recoverable"),
            ("data_access/job_posting_parser.py", "One broken metadata block"),
        ):
            with self.subTest(path=relative_path):
                source = (ROOT / "job_search" / relative_path).read_text(encoding="utf-8")
                self.assertIn(f"# {rationale}", source, "Parsing recovery needs an inline rationale")


if __name__ == "__main__":
    unittest.main()
