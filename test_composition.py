import unittest

import app
from job_search.presentation import legacy


class CompositionTests(unittest.TestCase):
    def test_root_composes_the_presentation_application(self):
        self.assertEqual(app.app.name, "job_search.presentation.legacy")
        self.assertIsNotNone(app.create_app)

    def test_factory_injects_service_without_constructing_flask_bound_dependencies(self):
        fake_service = object()
        web_app = legacy.create_app(dependencies=legacy.PresentationDependencies({"job_service": fake_service}))

        with web_app.app_context():
            self.assertIs(
                legacy.job_service(), fake_service, "The composition root must honor an injected service fake."
            )


if __name__ == "__main__":
    unittest.main()
