#!/usr/bin/env bash
# .claude/hooks/log-prompt.sh
#
# Logs every prompt submitted to Claude Code in this repo to one line per prompt to .ai/prompts.jsonl,
# along with the model, effort level, tool version and git state it ran against.
#
# Register it for three events in .claude/settings.json (see README-prompt-log.md):
#   SessionStart     -> records the session's starting model (when Claude Code provides it)
#   PostModelSwitch  -> records model changes made with /model or by Claude Code itself
#   UserPromptSubmit -> writes one JSON line per prompt
#
# Design rules:
#   * Never print to stdout. On UserPromptSubmit and SessionStart, stdout is added to Claude's context.
#   * Never block. Every exit is 0, so a logging failure can't stop a prompt.

exec 1>/dev/null            # anything accidentally printed goes nowhere
command -v jq >/dev/null 2>&1 || exit 0
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0

input=$(cat) || exit 0
event=$(jq -r '.hook_event_name // empty' <<<"$input" 2>/dev/null) || exit 0
session=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null) || exit 0
[ -n "$event" ] && [ -n "$session" ] || exit 0

LOG_FILE="${PROMPT_LOG_FILE:-$CLAUDE_PROJECT_DIR/.ai/prompts.jsonl}"
STATE_DIR="$CLAUDE_PROJECT_DIR/.claude/.prompt-log-state"
state="$STATE_DIR/$session.json"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

# Read a key from this session's state file (empty string if missing).
get_state() { jq -r --arg k "$1" '.[$k] // empty' "$state" 2>/dev/null; }

# Set a key in this session's state file.
set_state() {
  local cur tmp
  cur=$(cat "$state" 2>/dev/null); [ -n "$cur" ] || cur='{}'
  tmp="$state.$$"
  jq -c --arg k "$1" --arg v "$2" '.[$k] = $v' <<<"$cur" >"$tmp" 2>/dev/null && mv "$tmp" "$state"
}

case "$event" in
  SessionStart)
    model=$(jq -r '.model // empty' <<<"$input")
    if [ -n "$model" ]; then
      set_state model "$model"
      set_state model_source "session_start"
    fi
    ;;

  PostModelSwitch)
    model=$(jq -r '.to_model // empty' <<<"$input")
    if [ -n "$model" ]; then
      set_state model "$model"
      set_state model_source "model_switch"
    fi
    ;;

  UserPromptSubmit)
    # Claude Code version, cached per session so we only spawn `claude` once.
    version=$(get_state tool_version)
    if [ -z "$version" ] && command -v claude >/dev/null 2>&1; then
      version=$(claude --version 2>/dev/null | head -n1 | awk '{print $1}')
      [ -n "$version" ] && set_state tool_version "$version"
    fi

    # Git state at the moment the prompt was sent.
    commit="" branch=""
    if git -C "$CLAUDE_PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
      commit=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse HEAD 2>/dev/null)
      branch=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
    fi

    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || exit 0
    jq -c \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg model "$(get_state model)" \
      --arg effort "${CLAUDE_EFFORT:-}" \
      --arg version "$version" \
      --arg commit "$commit" \
      --arg branch "$branch" \
      --arg root "$CLAUDE_PROJECT_DIR" \
      '
      def nz: if . == "" then null else . end;
      {
        schema_version: 1,
        timestamp: $ts,
        provider: "anthropic",
        client: "claude-code",
        client_version: ($version | nz),
        model: ($model | nz),
        session_id,
        turn_id: (.prompt_id // null),
        permission_mode: (.permission_mode // null),
        effort: ($effort | nz),
        cwd: ((.cwd // "") | ltrimstr($root) | ltrimstr("/") | if . == "" then "." else . end),
        git: { head: ($commit | nz), branch: ($branch | nz), head_exact: true },
        source: "hook",
        prompt
      }' <<<"$input" >>"$LOG_FILE" 2>/dev/null
    ;;
esac

exit 0
