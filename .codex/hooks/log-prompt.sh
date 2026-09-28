#!/usr/bin/env bash
set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    echo "log-prompt: jq is required" >&2
    exit 1
fi

payload="$(cat)"

repo="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$repo" ]] || exit 0

cwd="$(jq -r '.cwd // empty' <<<"$payload")"

# Only record prompts actually associated with this repository.
case "$cwd/" in
    "$repo/"*) ;;
    *) exit 0 ;;
esac

log="$repo/.ai/prompts.jsonl"
mkdir -p "$(dirname "$log")"

timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

head="$(
    git -C "$repo" rev-parse HEAD 2>/dev/null || true
)"

branch="$(
    git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true
)"

relative_cwd="${cwd#"$repo"}"
relative_cwd="${relative_cwd#/}"
[[ -n "$relative_cwd" ]] || relative_cwd="."

jq -c \
    --arg timestamp "$timestamp" \
    --arg relative_cwd "$relative_cwd" \
    --arg head "$head" \
    --arg branch "$branch" \
    '
    {
        schema_version: 1,
        timestamp: $timestamp,

        provider: "openai",
        client: "codex-vscode",

        model: .model,
        session_id: .session_id,
        turn_id: .turn_id,
        permission_mode: .permission_mode,

        cwd: $relative_cwd,

        git: {
            head: (
                if $head == ""
                then null
                else $head
                end
            ),
            branch: (
                if $branch == ""
                then null
                else $branch
                end
            )
        },

        prompt: .prompt
    }
    ' <<<"$payload" >> "$log"