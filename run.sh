#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
INSTALL_MARKER="${VENV_DIR}/.requirements-installed"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

CODEX_CLI_ARG=""
NO_PROMPT=0
SETUP_ONLY=0
APP_ARGS=()

usage() {
  cat <<'EOF'
Usage:
  job-search-tool/run.sh [--codex-cli PATH] [--no-prompt] [--setup-only] [-v]

Options:
  --codex-cli PATH    Write PATH to job-search-tool/.env before starting.
  --codex-cli=PATH    Same as --codex-cli PATH.
  --api-key KEY       Ignored; scoring uses Codex CLI auth, not API keys.
  --api-key=KEY       Ignored; scoring uses Codex CLI auth, not API keys.
  --no-prompt         Do not prompt for CODEX_CLI_PATH when missing.
  --setup-only        Prepare venv/.env and exit without starting the app.
  -v, --verbose       Write non-error app logs to stdout (errors always go to stderr).
  -h, --help          Show this help.

Codex scoring uses your existing Codex CLI authentication.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --api-key" >&2
        exit 2
      fi
      echo "--api-key is ignored; scoring uses Codex CLI auth." >&2
      shift 2
      ;;
    --api-key=*)
      echo "--api-key is ignored; scoring uses Codex CLI auth." >&2
      shift
      ;;
    --codex-cli)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --codex-cli" >&2
        exit 2
      fi
      CODEX_CLI_ARG="$2"
      shift 2
      ;;
    --codex-cli=*)
      CODEX_CLI_ARG="${1#--codex-cli=}"
      shift
      ;;
    --no-prompt)
      NO_PROMPT=1
      shift
      ;;
    --setup-only)
      SETUP_ONLY=1
      shift
      ;;
    -v|--verbose)
      APP_ARGS+=("--verbose")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  fi
}

get_env_value() {
  local key="$1"
  if [[ ! -f "${ENV_FILE}" ]]; then
    return 0
  fi
  awk -F= -v key="${key}" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "${ENV_FILE}"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "${ENV_FILE}" ]] && grep -q "^${key}=" "${ENV_FILE}"; then
    while IFS= read -r line || [[ -n "${line}" ]]; do
      case "${line}" in
        "${key}="*) printf '%s=%s\n' "${key}" "${value}" >> "${tmp}" ;;
        *) printf '%s\n' "${line}" >> "${tmp}" ;;
      esac
    done < "${ENV_FILE}"
  else
    [[ -f "${ENV_FILE}" ]] && cat "${ENV_FILE}" > "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi
  mv "${tmp}" "${ENV_FILE}"
}

setup_venv() {
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Creating virtualenv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
  fi

  if [[ ! -f "${INSTALL_MARKER}" || "${REQUIREMENTS}" -nt "${INSTALL_MARKER}" ]]; then
    echo "Installing Python dependencies"
    "${PIP_BIN}" install -r "${REQUIREMENTS}"
    date > "${INSTALL_MARKER}"
  fi
}

prompt_for_codex_cli_if_needed() {
  local configured_path="${CODEX_CLI_PATH:-}"
  local env_path
  env_path="$(get_env_value CODEX_CLI_PATH)"

  if [[ -n "${CODEX_CLI_ARG}" ]]; then
    set_env_value CODEX_CLI_PATH "${CODEX_CLI_ARG}"
    export CODEX_CLI_PATH="${CODEX_CLI_ARG}"
    echo "Wrote CODEX_CLI_PATH to ${ENV_FILE}"
    return
  fi

  if [[ -n "${configured_path}" || -n "${env_path}" ]]; then
    return
  fi

  if command -v codex >/dev/null 2>&1; then
    set_env_value CODEX_CLI_PATH "codex"
    export CODEX_CLI_PATH="codex"
    echo "Using Codex CLI from PATH."
    return
  fi

  if [[ "${NO_PROMPT}" == "1" || ! -t 0 ]]; then
    echo "CODEX_CLI_PATH is not configured and codex was not found on PATH; Codex scoring will be unavailable."
    return
  fi

  cat <<'EOF'
CODEX_CLI_PATH is not configured and codex was not found on PATH.

The job CRM will work without it, but Codex scorecard generation requires the Codex CLI.

Install and authenticate the Codex CLI, then either ensure `codex` is on PATH or rerun with:
  job-search-tool/run.sh --codex-cli /path/to/codex

Press Enter to skip for now.
EOF
  printf "CODEX_CLI_PATH: "
  IFS= read -r entered_path
  if [[ -n "${entered_path}" ]]; then
    set_env_value CODEX_CLI_PATH "${entered_path}"
    export CODEX_CLI_PATH="${entered_path}"
    echo "Wrote CODEX_CLI_PATH to ${ENV_FILE}"
  else
    echo "Skipping Codex CLI setup. Codex scoring will be unavailable."
  fi
}

main() {
  cd "${REPO_ROOT}"
  ensure_env_file
  setup_venv
  prompt_for_codex_cli_if_needed

  if [[ "${SETUP_ONLY}" == "1" ]]; then
    echo "Setup complete."
    exit 0
  fi

  echo "Starting Job Search Console"
  echo "Open http://127.0.0.1:5050"
  exec "${PYTHON_BIN}" "${SCRIPT_DIR}/app.py" ${APP_ARGS[@]+"${APP_ARGS[@]}"}
}

main
