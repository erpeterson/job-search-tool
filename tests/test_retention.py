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
    assert "Dry run: 1 capture files would be archived" in output, output
    assert old.exists() and recent.exists(), "nothing is deleted without --yes"


def test_prune_with_yes_archives_old_captures_instead_of_deleting(workspace):
    import tarfile

    app_dir = workspace / "job-search-tool"
    old, recent = make_captures(app_dir, [40, 1])
    old_content = old.read_text()
    code, output = prune(app_dir, "--older-than", "30", "--yes")
    assert code == cli.EXIT_OK and "Archived 1 of 1 capture files into" in output, output
    assert not old.exists() and recent.exists(), "only captures older than the threshold are moved"
    [archive] = list((app_dir / "captures" / "archive").glob("captures-*.tar.gz"))
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.extractfile("svc/op/0.json").read().decode()
    assert member == old_content, "the pruned capture must be preserved in the archive"


def test_pruning_never_touches_existing_archives(workspace):
    app_dir = workspace / "job-search-tool"
    make_captures(app_dir, [40])
    prune(app_dir, "--older-than", "30", "--yes")
    code, output = prune(app_dir, "--older-than", "1", "--yes")
    assert "Archived 0 of 0 capture files." in output, f"archives are not re-pruned: {output}"
    assert len(list((app_dir / "captures" / "archive").glob("*.tar.gz"))) == 1, "the first archive is kept"


def test_incomplete_archive_keeps_originals(tmp_path, monkeypatch):
    import tarfile

    import pytest

    from job_search.data.captures import CaptureStore

    store = CaptureStore(tmp_path / "captures", lambda: True)
    path = store.write("svc", "op", {"a": 1}, {"text": "x"})
    real_open = tarfile.open

    class Truncated:
        def __init__(self, bundle):
            self.bundle = bundle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.bundle.close()

        def getnames(self):
            return []

    monkeypatch.setattr(
        "job_search.data.captures.tarfile.open",
        lambda name, mode: Truncated(real_open(name, mode)) if mode == "r:gz" else real_open(name, mode),
    )
    from job_search.domain.errors import StorageError

    with pytest.raises(StorageError):
        store.archive_files([path])
    assert path.exists(), "originals must be kept when the archive cannot be verified"
    assert list((tmp_path / "captures" / "archive").iterdir()) == [], "an unverified archive must not be left behind"


def test_prune_rejects_invalid_age(workspace, capsys):
    code, _ = prune(workspace / "job-search-tool", "--older-than", "0", "--yes")
    assert code == cli.EXIT_CONFIG_ERROR, f"an age below 1 day must be rejected, got {code}"
    assert "--older-than must be at least 1 day" in capsys.readouterr().err, (
        "expected '--older-than must be at least 1 day' in capsys.readouterr().err"
    )


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


def test_archive_write_failure_leaves_nothing_and_is_recorded(tmp_path, monkeypatch):
    import tarfile

    import pytest

    from job_search.data.captures import CaptureStore
    from job_search.domain.errors import StorageError
    from job_search.observability import METRICS

    store = CaptureStore(tmp_path / "captures", lambda: True)
    paths = [store.write("svc", "op", {"n": n}, {"text": "x"}) for n in range(3)]
    real_open = tarfile.open

    class FailingWriter:
        def __init__(self, bundle):
            self.bundle, self.added = bundle, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.bundle.close()

        def add(self, path, arcname):
            if self.added == 1:
                raise OSError(28, "No space left on device")
            self.bundle.add(path, arcname=arcname)
            self.added += 1

    monkeypatch.setattr(
        "job_search.data.captures.tarfile.open",
        lambda name, mode: FailingWriter(real_open(name, mode)) if mode == "w:gz" else real_open(name, mode),
    )
    before = METRICS.snapshot().get("blame.capture_archive_failed", 0)
    with pytest.raises(StorageError) as info:
        store.archive_files(paths)
    assert info.value.error_code == "capture_archive_failed", info.value.error_code
    assert list((tmp_path / "captures" / "archive").iterdir()) == [], "no partial archive may remain"
    assert all(path.exists() for path in paths), "every original capture must be kept"
    assert METRICS.snapshot()["blame.capture_archive_failed"] == before + 1, "the failure must be recorded"


def test_cli_reports_archive_failure_and_exits_nonzero(workspace, monkeypatch, capsys):
    from job_search.data.captures import CaptureStore
    from job_search.domain.errors import StorageError

    app_dir = workspace / "job-search-tool"
    make_captures(app_dir, [40])

    def fail(self, paths):
        raise StorageError("Capture archive could not be written; no captures were removed.", "capture_archive_failed")

    monkeypatch.setattr(CaptureStore, "archive_files", fail)
    code, _ = prune(app_dir, "--older-than", "30", "--yes")
    assert code == cli.EXIT_FAILURE, f"a failed archive must exit non-zero, got {code}"
    assert "Capture archive could not be written" in capsys.readouterr().err, "stderr explains the failure"
