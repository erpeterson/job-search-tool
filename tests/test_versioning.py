"""Version attribution: build id, CLI flag, /api/version, and schema user_version stamping."""

import sqlite3

import pytest

from job_search import cli, version
from job_search.data.database import Database


def test_build_id_combines_version_and_sha(tmp_path):
    assert version.build_id(tmp_path).startswith(f"{version.__version__}+")
    assert version.git_sha(tmp_path / "missing") == "unknown"


def test_version_flag_prints_build_id(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.parse_args(["--version"])
    assert exit_info.value.code == 0
    assert version.__version__ in capsys.readouterr().out


def test_api_version(client):
    payload = client.get("/api/version").get_json()
    assert payload["version"] == version.__version__
    assert payload["schema_version"] == version.SCHEMA_VERSION
    assert payload["build"].startswith(f"{version.__version__}+")


def _user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_fresh_database_is_stamped(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    Database(path).create_schema()
    assert _user_version(path) == version.SCHEMA_VERSION


def test_existing_v0_database_is_stamped_without_data_loss(tmp_path):
    path = tmp_path / "v0.sqlite3"
    Database(path).create_schema()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    before = conn.execute("SELECT count(*) FROM settings").fetchone()[0]
    conn.commit()
    conn.close()
    assert _user_version(path) == 0
    Database(path).create_schema()
    conn = sqlite3.connect(path)
    after = conn.execute("SELECT count(*) FROM settings").fetchone()[0]
    conn.close()
    assert _user_version(path) == version.SCHEMA_VERSION
    assert after == before
