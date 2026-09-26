"""Generated-data lifecycle: capture hygiene, capture pruning, and log archiving."""

import gzip
import io
import logging
import os
import time

from conftest import FakeHttp, FakeResolver

from job_search import cli
from job_search.data.captures import CaptureStore
from job_search.data.http_client import HttpClient
from job_search.observability import ArchivingRotatingFileHandler


def test_sensitive_response_headers_are_not_captured(tmp_path):
    store = CaptureStore(tmp_path / "captures", lambda: True)
    http = FakeHttp()
    http.route("x.com", text="ok", headers={"Set-Cookie": "session=secret", "Authorization": "Bearer t", "ETag": "1"})
    HttpClient(store, get=http, resolve=FakeResolver()).fetch("svc", "https://x.com/")
    [capture] = list((tmp_path / "captures").rglob("*.json"))
    text = capture.read_text(encoding="utf-8")
    assert "session=secret" not in text and "Bearer t" not in text, "credentials must not be written to captures"
    assert '"ETag"' in text, "non-sensitive headers are kept"


def make_captures(app_dir, ages_in_days):
    capture_dir = app_dir / "captures" / "svc" / "op"
    capture_dir.mkdir(parents=True)
    paths = []
    for index, age in enumerate(ages_in_days):
        path = capture_dir / f"{index}.json"
        path.write_text("{}", encoding="utf-8")
        stamp = time.time() - age * 86400
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def prune(app_dir, *args):
    out = io.StringIO()
    code = cli.main(["prune-captures", *args], environ={}, out=out, app_dir=app_dir)
    return code, out.getvalue()


def test_prune_without_yes_is_a_dry_run(workspace):
    app_dir = workspace / "job-search-tool"
    old, recent = make_captures(app_dir, [40, 1])
    code, output = prune(app_dir, "--older-than", "30")
    assert code == cli.EXIT_OK, f"dry run should succeed, got {code}"
    assert str(old) in output and str(recent) not in output, f"only old captures are listed: {output}"
    assert "Dry run: 1 capture files would be deleted" in output, output
    assert old.exists() and recent.exists(), "nothing is deleted without --yes"


def test_prune_with_yes_deletes_only_old_captures(workspace):
    app_dir = workspace / "job-search-tool"
    old, recent = make_captures(app_dir, [40, 1])
    code, output = prune(app_dir, "--older-than", "30", "--yes")
    assert code == cli.EXIT_OK and "Deleted 1 of 1 capture files." in output, output
    assert not old.exists() and recent.exists(), "only captures older than the threshold are deleted"


def test_prune_rejects_invalid_age(workspace, capsys):
    code, _ = prune(workspace / "job-search-tool", "--older-than", "0", "--yes")
    assert code == cli.EXIT_CONFIG_ERROR, f"an age below 1 day must be rejected, got {code}"
    assert "--older-than must be at least 1 day" in capsys.readouterr().err


def test_rotated_logs_are_archived_not_deleted(tmp_path):
    path = tmp_path / "events.log"
    handler = ArchivingRotatingFileHandler(path, max_bytes=200)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("tests.archive")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for index in range(30):
            logger.info("line %03d %s", index, "x" * 40)
            time.sleep(0.001)  # distinct archive timestamps
    finally:
        logger.removeHandler(handler)
        handler.close()
    archives = sorted(tmp_path.glob("events.log.*.gz"))
    assert len(archives) >= 5, f"every rollover should produce an archive: {archives}"
    archived = path.read_text()
    for archive in archives:
        with gzip.open(archive, "rt") as handle:
            archived += handle.read()
    missing = [index for index in range(30) if f"line {index:03d}" not in archived]
    assert not missing, f"no log lines may be lost across rotations: {missing}"
