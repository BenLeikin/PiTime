#!/usr/bin/env bash
# Run the project's checks. CI runs exactly this; run it before pushing.
#
#   tools/check.sh             every check whose tool is installed
#   tools/check.sh python      Python tests and ruff
#   tools/check.sh shell       shell syntax and shellcheck
#   tools/check.sh js          JavaScript syntax and tests (needs node)
#
# A check whose tool is missing is skipped with a note. Set CHECK_STRICT=1
# (CI does) to make a missing tool a failure instead.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

SHELL_SCRIPTS=(install.sh deploy.sh tools/check.sh usr/local/sbin/timeserver-tune
               scripts/convert-to-chrony.sh scripts/sync-nts-cert.sh.example)
failed=0

step() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
run() { step "$*"; "$@" || failed=1; }
need() {
  command -v "$1" >/dev/null && return 0
  if [[ ${CHECK_STRICT:-0} == 1 ]]; then
    echo "missing tool: $1" >&2
    failed=1
  else
    echo "    skipped ($1 is not installed)"
  fi
  return 1
}

check_python() {
  run python3 -m unittest discover -s tests
  need ruff && run ruff check .
}

check_shell() {
  step "bash -n (syntax)"
  for script in "${SHELL_SCRIPTS[@]}"; do
    bash -n "$script" || failed=1
  done
  need shellcheck && run shellcheck "${SHELL_SCRIPTS[@]}"
}

check_js() {
  need node || return
  run node --check dashboard/static/dashboard.js
  run node --test tests/test_page.js
}

[[ $# -eq 0 ]] && set -- python shell js
for group in "$@"; do
  case "$group" in
    python) check_python ;;
    shell) check_shell ;;
    js) check_js ;;
    *) echo "unknown check: $group (python, shell or js)" >&2; exit 2 ;;
  esac
done

if [[ $failed -ne 0 ]]; then
  echo "CHECKS FAILED" >&2
  exit 1
fi
step "all checks passed"
