#!/usr/bin/env bash
# .claude/hooks/log-prompt.sh
#
# Logs every prompt submitted to Claude Code in this repo to prompts/<date>-<session_id>.jsonl,
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

LOG_DIR="${PROMPT_LOG_DIR:-$CLAUDE_PROJECT_DIR/prompts}"
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
    # Pick this session's log file once, so every prompt in the session lands in the same file.
    file=$(get_state log_file)
    if [ -z "$file" ]; then
      file="$(date -u +%Y-%m-%d)-$session.jsonl"
      set_state log_file "$file"
    fi

    # Claude Code version, cached per session so we only spawn `claude` once.
    version=$(get_state tool_version)
    if [ -z "$version" ] && command -v claude >/dev/null 2>&1; then
      version=$(claude --version 2>/dev/null | head -n1 | awk '{print $1}')
      [ -n "$version" ] && set_state tool_version "$version"
    fi

    # Git state at the moment the prompt was sent.
    commit="" branch="" dirty="null"
    if git -C "$CLAUDE_PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
      commit=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse HEAD 2>/dev/null)
      branch=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
      # Ignore the prompt log itself when deciding whether the tree is dirty.
      if [ -n "$(git -C "$CLAUDE_PROJECT_DIR" status --porcelain -- . ":(exclude)prompts" ":(exclude).claude/.prompt-log-state" 2>/dev/null)" ]; then
        dirty="true"; else dirty="false"; fi
    fi

    mkdir -p "$LOG_DIR" 2>/dev/null || exit 0
    jq -c \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg model "$(get_state model)" \
      --arg model_source "$(get_state model_source)" \
      --arg effort "${CLAUDE_EFFORT:-}" \
      --arg version "$version" \
      --arg commit "$commit" \
      --arg branch "$branch" \
      --argjson dirty "$dirty" \
      --arg root "$CLAUDE_PROJECT_DIR" \
      '
      def nz: if . == "" then null else . end;
      {
        schema: 1,
        source: "hook",
        ts: $ts,
        tool: "claude-code",
        tool_version: ($version | nz),
        session_id,
        prompt_id: (.prompt_id // null),
        model: ($model | nz),
        model_source: (if $model == "" then "unknown" else $model_source end),
        effort: ($effort | nz),
        permission_mode: (.permission_mode // null),
        git: { commit: ($commit | nz), commit_exact: true, branch: ($branch | nz), dirty: $dirty },
        cwd: ((.cwd // "") | ltrimstr($root) | ltrimstr("/") | if . == "" then "." else . end),
        prompt
      }' <<<"$input" >>"$LOG_DIR/$file" 2>/dev/null
    ;;
esac

exit 0
