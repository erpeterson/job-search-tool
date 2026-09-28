#!/usr/bin/env bash
set -euo pipefail

repo=""
branch=""
output=""
dry_run=0

usage() {
  cat <<'EOF'
Usage: backfill-prompts.sh [options]

Options:
  --repo PATH       Repository to backfill (default: current Git repository)
  --branch NAME     Only sessions created on this branch (default: current branch)
  --output PATH     Output JSONL (default: <repo>/.ai/prompts.jsonl)
  --dry-run         Print matching records instead of modifying the output file
  -h, --help        Show this help
EOF
}

while (($#)); do
  case "$1" in
    --repo)   repo="${2:?missing value for --repo}"; shift 2 ;;
    --branch) branch="${2:?missing value for --branch}"; shift 2 ;;
    --output) output="${2:?missing value for --output}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || {
  echo "git is required" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required" >&2
  exit 1
}

if [[ -z "$repo" ]]; then
  repo="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Not inside a Git repository; pass --repo PATH" >&2
    exit 1
  }
else
  repo="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Not a Git repository: $repo" >&2
    exit 1
  }
fi

if [[ -z "$branch" ]]; then
  branch="$(git -C "$repo" branch --show-current)"

  [[ -n "$branch" ]] || {
    echo "Current checkout is detached; pass --branch NAME" >&2
    exit 1
  }
fi

if [[ -z "$output" ]]; then
  output="$repo/.ai/prompts.jsonl"
elif [[ "$output" != /* ]]; then
  output="$repo/$output"
fi

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}" \
REPO="$repo" \
BRANCH="$branch" \
OUTPUT="$output" \
DRY_RUN="$dry_run" \
python3 - <<'PY'
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(os.environ["REPO"]).resolve()
branch = os.environ["BRANCH"]
output = Path(os.environ["OUTPUT"])
codex_home = Path(os.environ["CODEX_HOME"]).expanduser()
dry_run = os.environ["DRY_RUN"] == "1"

roots = [
    codex_home / "sessions",
    codex_home / "archived_sessions",
]


def warn(message):
    print(f"backfill-prompts: {message}", file=sys.stderr)


def read_rollout(path):
    """Yield lines from plain or zstd-compressed Codex rollouts."""

    if path.name.endswith(".jsonl.zst"):
        zstd = shutil.which("zstd")

        if not zstd:
            warn(
                "skipping compressed rollout "
                f"(install zstd to read it): {path}"
            )
            return

        proc = subprocess.Popen(
            [zstd, "-q", "-dc", str(path)],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        assert proc.stdout is not None

        try:
            yield from proc.stdout
        finally:
            proc.stdout.close()
            rc = proc.wait()

            if rc != 0:
                warn(f"zstd failed for {path} (exit {rc})")

    else:
        try:
            with path.open(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as f:
                yield from f

        except OSError as e:
            warn(f"cannot read {path}: {e}")


def under_repo(cwd):
    if not cwd:
        return False

    try:
        candidate = Path(cwd).expanduser().resolve(strict=False)

        return (
            os.path.commonpath([str(repo), str(candidate)])
            == str(repo)
        )

    except (OSError, ValueError):
        return False


def relative_cwd(cwd):
    try:
        candidate = Path(cwd).expanduser().resolve(strict=False)

        if (
            os.path.commonpath([str(repo), str(candidate)])
            == str(repo)
        ):
            rel = os.path.relpath(str(candidate), str(repo))
            return "." if rel == "." else rel

    except (OSError, ValueError):
        pass

    return None


def source_name(source):
    if isinstance(source, str):
        return source

    if isinstance(source, dict):
        # SessionSource representation has changed over time.
        for key in ("custom", "type", "name"):
            value = source.get(key)

            if isinstance(value, str):
                return value

    return "unknown"


def client_name(source):
    if source == "vscode":
        return "codex-vscode"

    return f"codex-{source}"


def stable_key(record):
    """
    Prefer Codex's session+turn identity.

    Older records without turn IDs fall back to timestamp+prompt.
    """

    sid = record.get("session_id")
    tid = record.get("turn_id")

    if sid and tid:
        return ("turn", sid, tid)

    return (
        "fallback",
        sid,
        record.get("timestamp"),
        record.get("prompt"),
    )


def rollout_paths():
    """
    Scan active and archived Codex rollouts.

    Prefer an uncompressed copy if both .jsonl and .jsonl.zst exist.
    """

    seen = set()

    for root in roots:
        if not root.exists():
            continue

        for pattern in (
            "rollout-*.jsonl",
            "rollout-*.jsonl.zst",
        ):
            for path in root.rglob(pattern):
                logical = str(path)

                if logical.endswith(".zst"):
                    logical = logical[:-4]

                if logical in seen:
                    continue

                plain = Path(logical)
                chosen = plain if plain.exists() else path

                seen.add(logical)
                yield chosen


def parse_rollout(path):
    meta = None
    current_turn = None
    records = []

    for line_no, line in enumerate(
        read_rollout(path) or (),
        1,
    ):
        line = line.strip()

        if not line:
            continue

        try:
            item = json.loads(line)

        except json.JSONDecodeError as e:
            warn(f"invalid JSON in {path}:{line_no}: {e}")
            continue

        typ = item.get("type")
        payload = item.get("payload") or {}

        if typ == "session_meta":
            meta = payload
            continue

        if meta is None:
            continue

        git = meta.get("git") or {}

        # Git branch is captured when the session is created.
        if git.get("branch") != branch:
            return []

        if typ == "turn_context":
            current_turn = payload
            continue

        if not (
            typ == "event_msg"
            and payload.get("type") == "user_message"
            and isinstance(payload.get("message"), str)
            and payload.get("message")
        ):
            continue

        turn = current_turn or {}
        cwd = turn.get("cwd") or meta.get("cwd")

        # A session can change working directories. Only capture turns
        # that actually operated inside this repository.
        if not under_repo(cwd):
            continue

        source = source_name(meta.get("source"))

        permission = turn.get("active_permission_profile")

        if not isinstance(permission, str):
            permission = turn.get("permission_profile")

        if not isinstance(permission, str):
            permission = None

        session_id = (
            meta.get("session_id")
            or meta.get("id")
        )

        provider = (
            meta.get("model_provider")
            or "openai"
        )

        record = {
            "schema_version": 1,
            "timestamp": item.get("timestamp"),

            "provider": provider,
            "client": client_name(source),

            "model": turn.get("model"),
            "session_id": session_id,
            "turn_id": turn.get("turn_id"),

            "permission_mode": permission,
            "approval_policy": turn.get("approval_policy"),
            "reasoning_effort": turn.get("effort"),

            "cwd": relative_cwd(cwd),

            "git": {
                "head": git.get("commit_hash"),
                "branch": git.get("branch"),
            },

            "prompt": payload["message"],

            "capture": {
                "method": "backfill",

                # Important: historical Git metadata comes from
                # session_meta, not from the instant this prompt
                # was submitted.
                "git_metadata_scope": "session_start",

                "codex_source": source,
                "codex_originator": meta.get("originator"),
                "codex_version": meta.get("cli_version"),
            },
        }

        # Some Codex versions classify user-message events.
        if payload.get("kind") is not None:
            record["prompt_kind"] = payload.get("kind")

        records.append(record)

    return records


historical = []

for path in rollout_paths():
    historical.extend(parse_rollout(path))


# Read records already captured by the live hook.
existing = []

if output.exists():
    with output.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue

            try:
                existing.append(json.loads(line))

            except json.JSONDecodeError as e:
                raise SystemExit(
                    f"Invalid JSON in {output}:{line_no}: {e}"
                )


# Merge and deduplicate.
merged = []
keys = set()

for record in existing + historical:
    key = stable_key(record)

    if key in keys:
        continue

    keys.add(key)
    merged.append(record)


# The one-time backfill rewrites the corpus chronologically.
# Subsequent hook writes remain append-only.
merged.sort(
    key=lambda r: (
        r.get("timestamp") or "",
        r.get("session_id") or "",
        r.get("turn_id") or "",
    )
)

added = len(merged) - len(existing)


if dry_run:
    existing_keys = {
        stable_key(record)
        for record in existing
    }

    for record in historical:
        if stable_key(record) not in existing_keys:
            print(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

    warn(
        f"would add {added} prompt(s) "
        f"for branch {branch!r}"
    )

    raise SystemExit(0)


# Atomic rewrite so an interrupted backfill cannot truncate the log.
output.parent.mkdir(parents=True, exist_ok=True)

fd, tmp_name = tempfile.mkstemp(
    prefix=output.name + ".",
    suffix=".tmp",
    dir=output.parent,
)

try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for record in merged:
            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            f.write("\n")

    os.replace(tmp_name, output)

except Exception:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass

    raise


print(
    f"Backfilled {added} prompt(s) "
    f"for branch {branch!r} into {output}",
    file=sys.stderr,
)
PY