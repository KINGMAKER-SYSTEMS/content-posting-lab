#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: ralph_loop_context_wipe.sh ISSUE_ID PASS_NUMBER [REPO_ROOT]" >&2
  exit 64
fi

ISSUE_ID="$1"
PASS_NUMBER="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${3:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

python "${SCRIPT_DIR}/ralph_loop.py" wipe \
  "${ISSUE_ID}" \
  --repo-root "${REPO_ROOT}" \
  --pass-number "${PASS_NUMBER}"
