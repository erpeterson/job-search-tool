"""Web process startup at the presentation boundary."""


def main(application):
    dependencies = application.extensions["job_search.dependencies"]
    dependencies.startup_service.initialize()
    configuration = dependencies.configuration
    print(f"Job Search Console running at http://{configuration.settings.host}:{configuration.settings.port}")
    print(f"Database: {dependencies.database_path}")
    application.run(
        host=configuration.settings.host,
        port=configuration.settings.port,
        debug=configuration.settings.debug,
        use_reloader=False,
    )
