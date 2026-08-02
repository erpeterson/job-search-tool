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

API_KEY_ARG=""
NO_PROMPT=0
SETUP_ONLY=0

usage() {
  cat <<'EOF'
Usage:
  job-search-tool/run.sh [--api-key KEY] [--no-prompt] [--setup-only]

Options:
  --api-key KEY       Write KEY to job-search-tool/.env before starting.
  --api-key=KEY       Same as --api-key KEY.
  --no-prompt         Do not prompt for OPENAI_API_KEY when missing.
  --setup-only        Prepare venv/.env and exit without starting the app.
  -h, --help          Show this help.

OPENAI_API_KEY is optional for CRM usage, but required for GPT scoring.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --api-key" >&2
        exit 2
      fi
      API_KEY_ARG="$2"
      shift 2
      ;;
    --api-key=*)
      API_KEY_ARG="${1#--api-key=}"
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

prompt_for_api_key_if_needed() {
  local configured_key="${OPENAI_API_KEY:-}"
  local env_key
  env_key="$(get_env_value OPENAI_API_KEY)"

  if [[ -n "${API_KEY_ARG}" ]]; then
    set_env_value OPENAI_API_KEY "${API_KEY_ARG}"
    export OPENAI_API_KEY="${API_KEY_ARG}"
    echo "Wrote OPENAI_API_KEY to ${ENV_FILE}"
    return
  fi

  if [[ -n "${configured_key}" || -n "${env_key}" ]]; then
    return
  fi

  if [[ "${NO_PROMPT}" == "1" || ! -t 0 ]]; then
    echo "OPENAI_API_KEY is not configured; GPT scoring will be unavailable."
    return
  fi

  cat <<'EOF'
OPENAI_API_KEY is not configured.

The job CRM will work without it, but GPT scorecard generation requires an OpenAI API key.

To create one:
  1. Go to https://platform.openai.com/api-keys
  2. Sign in.
  3. Create a new secret key.
  4. Paste it here, or rerun with:
     job-search-tool/run.sh --api-key YOUR_KEY

Press Enter to skip for now.
EOF
  printf "OPENAI_API_KEY: "
  IFS= read -r entered_key
  if [[ -n "${entered_key}" ]]; then
    set_env_value OPENAI_API_KEY "${entered_key}"
    export OPENAI_API_KEY="${entered_key}"
    echo "Wrote OPENAI_API_KEY to ${ENV_FILE}"
  else
    echo "Skipping API key setup. GPT scoring will be unavailable."
  fi
}

main() {
  cd "${REPO_ROOT}"
  ensure_env_file
  setup_venv
  prompt_for_api_key_if_needed

  if [[ "${SETUP_ONLY}" == "1" ]]; then
    echo "Setup complete."
    exit 0
  fi

  echo "Starting Job Search Console"
  echo "Open http://127.0.0.1:5050"
  exec "${PYTHON_BIN}" "${SCRIPT_DIR}/app.py"
}

main
