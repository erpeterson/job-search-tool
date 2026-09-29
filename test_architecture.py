"""Regression checks for the three-tier boundary required by AGENTS.md."""

import ast
import unittest
from dataclasses import fields
from pathlib import Path

from job_search.presentation.dependencies import PresentationDependencies

ROOT = Path(__file__).resolve().parent
APPLICATION = ROOT / "job_search" / "application"
PRESENTATION = ROOT / "job_search" / "presentation"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_presentation_does_not_construct_storage_transport_or_services(self):
        forbidden_calls = {
            "infrastructure",
            "database_session",
            "open_connection",
            "outbound_clients",
            "codex_json_gateway",
        }
        forbidden_names = {"INFRASTRUCTURE", "OUTBOUND_HTTP_CLIENT", "OUTBOUND_CLIENTS", "CODEX_JSON_GATEWAY"}
        for path in PRESENTATION.rglob("*.py"):
            with self.subTest(module=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                assigned = {
                    target.id
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                    if isinstance(target, ast.Name)
                }
                self.assertFalse(
                    assigned & forbidden_names,
                    f"{path.name} must receive concrete dependencies from composition.",
                )
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertFalse(
                        name in forbidden_calls or name.endswith(("Repository", "Gateway", "Service")),
                        f"{path.name} must not construct {name} at the presentation boundary.",
                    )

    def test_presentation_dependency_lookups_match_required_named_contract(self):
        contract_fields = {field.name for field in fields(PresentationDependencies)}
        self.assertTrue(contract_fields, "The web contract must declare required named dependencies.")
        for path in PRESENTATION.rglob("*.py"):
            with self.subTest(module=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertFalse(
                    any(isinstance(node, ast.Attribute) and node.attr == "services" for node in ast.walk(tree)),
                    f"{path.name} must not use a permissive service dictionary.",
                )
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                        continue
                    if node.func.id != "dependency":
                        continue
                    self.assertEqual(len(node.args), 1, "Dependency lookups must not accept a fallback.")
                    self.assertIsInstance(node.args[0], ast.Constant, "Dependency names must be static.")
                    self.assertIn(
                        node.args[0].value,
                        contract_fields,
                        "Every presentation dependency must be required at app construction.",
                    )

    def test_presentation_does_not_own_runtime_or_filesystem_adapters(self):
        forbidden_imports = {"os", "shutil", "dotenv", "logging", "pathlib", "bs4", "requests", "sqlite3", "subprocess"}
        filesystem_calls = {
            "read_text",
            "write_text",
            "mkdir",
            "glob",
            "rglob",
            "unlink",
            "rename",
            "resolve",
            "relative_to",
        }
        for path in PRESENTATION.rglob("*.py"):
            with self.subTest(module=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertFalse(
                    forbidden_imports & _imports(path),
                    f"{path.name} must receive runtime and filesystem adapters from composition.",
                )
                self.assertFalse(
                    any(
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in filesystem_calls
                        for node in ast.walk(tree)
                    ),
                    f"{path.name} must not perform direct filesystem operations.",
                )

    def test_application_services_do_not_import_framework_or_concrete_infrastructure(self):
        forbidden = {"flask", "sqlite3", "requests", "subprocess"}
        for path in APPLICATION.glob("*.py"):
            with self.subTest(module=path.name):
                imported = _imports(path)
                self.assertFalse(
                    forbidden & imported,
                    f"{path.name} must depend on ports, not framework or concrete infrastructure modules.",
                )

    def test_root_is_limited_to_composition_and_startup(self):
        root_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertLessEqual(
            root_source.count("\n"), 30, "The root entry point must remain a thin composition boundary."
        )
        self.assertIn("job_search.presentation.cli", root_source)

    def test_legacy_presentation_module_is_removed(self):
        self.assertFalse((PRESENTATION / "legacy.py").exists(), "The route migration must remove legacy.py.")

    def test_every_presentation_module_has_no_storage_or_transport_dependencies(self):
        forbidden_imports = {"bs4", "requests", "sqlite3", "subprocess"}
        for path in PRESENTATION.rglob("*.py"):
            with self.subTest(module=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = _imports(path)
                self.assertFalse(
                    forbidden_imports & imports,
                    f"{path.name} must receive concrete adapters from composition rather than import them.",
                )
                self.assertFalse(
                    any(
                        isinstance(node, ast.ImportFrom)
                        and node.module
                        and node.module.startswith("job_search.data_access")
                        for node in ast.walk(tree)
                    ),
                    f"{path.name} must not import data-access implementations.",
                )
                self.assertFalse(
                    any(
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"executemany", "executescript"}
                        or (
                            isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "execute"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in {"conn", "connection"}
                        )
                        for node in ast.walk(tree)
                    ),
                    f"{path.name} must not contain SQL execution.",
                )
                self.assertFalse(
                    any(
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "connect"
                        for node in ast.walk(tree)
                    ),
                    f"{path.name} must receive database sessions through composition rather than define a connection opener.",
                )
                self.assertFalse(
                    any(
                        isinstance(node, ast.With)
                        and any(
                            isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Name)
                            and item.context_expr.func.id == "connect"
                            for item in node.items
                        )
                        for node in ast.walk(tree)
                    ),
                    f"{path.name} must not acquire database sessions inside presentation workflows.",
                )

    def test_page_markup_lives_in_the_presentation_template_package(self):
        template = ROOT / "job_search" / "presentation" / "templates" / "index.html"
        self.assertTrue(template.is_file(), "The web page must be a version-controlled presentation template.")
        self.assertIn("Job Search Console", template.read_text(encoding="utf-8"))
        self.assertNotIn("INDEX_HTML", (PRESENTATION / "routes.py").read_text(encoding="utf-8"))

    def test_web_process_does_not_start_background_workers_or_schedulers(self):
        source = (PRESENTATION / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn(".submit(", source, "HTTP task submission must only persist queued work.")
        self.assertNotIn("scheduler_loop", source, "Web startup must not own a scheduler loop.")

    def test_managed_processes_do_not_import_presentation(self):
        for name in ("worker.py", "scheduler.py"):
            with self.subTest(module=name):
                tree = ast.parse((ROOT / "job_search" / name).read_text(encoding="utf-8"))
                imported = [
                    node.module
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module is not None
                ]
                self.assertFalse(
                    any(module.startswith("job_search.presentation") for module in imported),
                    f"{name} must compose application services without presentation callbacks.",
                )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.partition(".")[0])
    return names
