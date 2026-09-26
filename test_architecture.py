"""Regression checks for the three-tier boundary required by AGENTS.md."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPLICATION = ROOT / "job_search" / "application"
PRESENTATION = ROOT / "job_search" / "presentation" / "legacy.py"


class ArchitectureBoundaryTests(unittest.TestCase):
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
            root_source.count("\n"), 20, "The root entry point must remain a thin composition boundary."
        )
        self.assertIn("job_search.presentation.legacy", root_source)

    def test_route_definitions_do_not_issue_sql(self):
        source = PRESENTATION.read_text(encoding="utf-8")
        routes = source[source.index('@app.get("/")') : source.index("def scheduler_loop")]
        self.assertNotIn(".execute(", routes, "HTTP handlers must call services/repositories rather than issue SQL.")

    def test_page_markup_lives_in_the_presentation_template_package(self):
        template = ROOT / "job_search" / "presentation" / "templates" / "index.html"
        self.assertTrue(template.is_file(), "The web page must be a version-controlled presentation template.")
        self.assertIn("Job Search Console", template.read_text(encoding="utf-8"))
        self.assertNotIn("INDEX_HTML", PRESENTATION.read_text(encoding="utf-8"))

    def test_web_process_does_not_start_background_workers_or_schedulers(self):
        source = PRESENTATION.read_text(encoding="utf-8")
        startup = source[source.index("def start_background_task") : source.index("def scheduler_loop")]
        scheduler = source[source.index("def start_scheduler") : source.index("def main")]
        self.assertNotIn(".submit(", startup, "HTTP task submission must only persist queued work.")
        self.assertNotIn("Thread(", scheduler, "Web startup must not start a scheduler thread.")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.partition(".")[0])
    return names
