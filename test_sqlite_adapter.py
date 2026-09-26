import tempfile
import unittest
from pathlib import Path

from job_search.data_access.schema import initialize_schema
from job_search.data_access.sqlite import connection


class SqliteConnectionTests(unittest.TestCase):
    def test_connection_commits_and_closes_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            with connection(database_path) as database:
                database.execute("CREATE TABLE values_table (value TEXT)")
                database.execute("INSERT INTO values_table(value) VALUES ('saved')")
            with connection(database_path) as database:
                value = database.execute("SELECT value FROM values_table").fetchone()["value"]

        self.assertEqual(value, "saved")

    def test_schema_initializer_creates_required_tables_and_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            with connection(database_path) as database:
                initialize_schema(database)
                tables = {
                    row["name"] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                indexes = {
                    row["name"] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
                }

        self.assertTrue({"jobs", "search_queries", "search_runs", "discovered_jobs"}.issubset(tables))
        self.assertIn("idx_jobs_url", indexes)


if __name__ == "__main__":
    unittest.main()
