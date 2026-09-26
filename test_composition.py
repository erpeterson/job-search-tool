import unittest

import app
from job_search.composition import discovery_service, task_execution_service
from job_search.presentation import legacy
from job_search.presentation.dependencies import PresentationDependencies
from job_search.presentation.factory import create_app


class CompositionTests(unittest.TestCase):
    def test_root_composes_the_presentation_application(self):
        self.assertEqual(app.app.name, "job_search.presentation.factory")
        self.assertIsNotNone(app.create_app)

    def test_factory_injects_service_without_constructing_flask_bound_dependencies(self):
        fake_service = object()
        web_app = create_app(
            dependencies=PresentationDependencies({"job_service": fake_service}), route_blueprint=legacy.routes
        )

        with web_app.app_context():
            self.assertIs(
                legacy.job_service(), fake_service, "The composition root must honor an injected service fake."
            )

    def test_task_processor_is_composed_from_application_services(self):
        class Console:
            def job(self, job_id):
                return {"id": job_id}

        class Scoring:
            def populate_by_id(self, job_id, *, force_refresh):
                self.called = (job_id, force_refresh)
                return {"total_score": 91}

        class Packets:
            def generate(self, job_id):
                return {"path": f"applications/{job_id}"}

        scoring = Scoring()
        service = task_execution_service(Console(), scoring, Packets())

        self.assertEqual(service.process({"operation": "scorecards", "job_id": 3}), ("complete", "Codex score 91"))
        self.assertEqual(scoring.called, (3, False))

    def test_discovery_workflow_receives_explicit_operations(self):
        service = discovery_service(
            "Unknown",
            scoring_enabled=lambda: False,
            scorer_available=lambda: True,
            scorer_path=lambda: "codex",
            log=lambda *_args, **_kwargs: None,
            score=lambda *_args, **_kwargs: {},
            create=lambda *_args, **_kwargs: 1,
            apply_filter=lambda *_args, **_kwargs: None,
            now=lambda: 1,
            normalize_pipeline=lambda value, fallback: value or fallback,
            refinement_context=lambda *_args: [],
            refinement_prompt=lambda *_args: {},
            refine=lambda *_args, **_kwargs: None,
            clean_text=lambda value: value,
            update_query=lambda *_args, **_kwargs: None,
        )
        self.assertIsNotNone(service)


if __name__ == "__main__":
    unittest.main()
