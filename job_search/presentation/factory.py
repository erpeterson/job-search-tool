"""Flask application factory and route registration."""

from __future__ import annotations

from typing import Any

from flask import Flask

from job_search.presentation.dependencies import PresentationDependencies
from job_search.presentation.legacy import routes


def create_app(
    test_config: dict[str, Any] | None = None,
    dependencies: PresentationDependencies | None = None,
    *,
    route_blueprint: Any = routes,
) -> Flask:
    """Create the delivery application with services supplied by composition."""
    if dependencies is None:
        raise ValueError("Web dependencies must be supplied by the composition root.")
    application = Flask(__name__)
    if test_config:
        application.config.update(test_config)
    application.extensions["job_search.dependencies"] = dependencies
    application.register_blueprint(route_blueprint)
    return application
