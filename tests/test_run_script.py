"""run.sh and .env.example configuration handling."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from job_search.config import AppConfig
from job_search.data.env_file import EnvFile

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="run.sh requires bash")
def test_run_script_rejects_old_python(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        '#!/bin/sh\ncase "$2" in *version_info\\ \\>=*) exit 1 ;; esac\necho "3.11.9"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed arguments: the repo's run.sh under a test PATH
        [shutil.which("bash"), str(REPO / "run.sh"), "--setup-only", "--no-prompt"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}: {result.stderr}"
    assert "Python 3.12+ is required; python3 is 3.11.9." in result.stderr, result.stderr
    assert result.stdout == "", f"errors must go to stderr, stdout was: {result.stdout!r}"


def test_env_example_lists_every_supported_variable():
    code_vars = set()
    for path in (REPO / "job_search").rglob("*.py"):
        code_vars |= set(re.findall(r'"((?:JOB_SEARCH|CODEX|PANDOC)_[A-Z_]+)"', path.read_text(encoding="utf-8")))
    example_vars = set(EnvFile(REPO / ".env.example").read())
    missing = sorted(code_vars - example_vars)
    assert not missing, f".env.example is missing supported variables: {missing}"


def test_env_example_is_a_valid_configuration(tmp_path):
    settings = EnvFile(REPO / ".env.example").read()
    config = AppConfig.from_env(settings, app_dir=tmp_path / "app")
    assert (config.host, config.port) == ("127.0.0.1", 5050), "the example must load with its documented defaults"


def test_readme_documents_every_route():
    from flask import Flask

    from job_search.web.routes import bp

    app = Flask("docs-check")
    app.register_blueprint(bp)
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    missing = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = re.sub(r"<(?:int:)?([a-z_]+)>", r"<\1>", rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            if f"| {method} | `{path}` |" not in readme:
                missing.append(f"{method} {path}")
    assert not missing, f"README HTTP API table is missing: {missing}"
