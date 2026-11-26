#!/usr/bin/env bash
# Push this checkout to the Pi over SSH and run install.sh there.
#
#   ./deploy.sh                    # dashboard + systemd unit
#   ./deploy.sh --diff             # only show how the Pi's live files differ
#   ./deploy.sh --configs          # also chrony/gpsd/boot configs (shows a diff, asks first)
#   ./deploy.sh --configs --yes    # same, without the prompt
#
# Target: $PI_HOST (default "timeserver", an alias in ~/.ssh/config).
# The remote user needs sudo; passwordless sudo lets this run unattended.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PI_HOST="${PI_HOST:-timeserver}"
STAGE=".opentimeserver-deploy"
MODE=install
CONFIGS=0
YES=0

for arg in "$@"; do
  case "$arg" in
    --diff) MODE=diff ;;
    --configs) CONFIGS=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

FILES=(
  install.sh
  dashboard/chrony_dashboard.py
  systemd/chrony-webpage.service
  etc/chrony/chrony.conf
  etc/default/gpsd
  boot/firmware/config.txt
)

if command -v git >/dev/null && git rev-parse --git-dir >/dev/null 2>&1; then
  REV="$(git rev-parse --short HEAD)"
  if ! git diff --quiet HEAD -- "${FILES[@]}"; then
    REV="$REV+uncommitted"
    echo "warning: deploying uncommitted changes" >&2
  fi
else
  REV=unknown
fi

python3 -m py_compile dashboard/chrony_dashboard.py

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI_HOST")

log "Uploading $REV to $PI_HOST:~/$STAGE"
tar -cf - "${FILES[@]}" | "${SSH[@]}" "rm -rf ~/$STAGE && mkdir -p ~/$STAGE && tar -xf - -C ~/$STAGE && echo '$REV' > ~/$STAGE/REVISION"

# Compare staged files against what is live on the Pi.
remote_diff() {
  "${SSH[@]}" "cd ~/$STAGE && for pair in \
      dashboard/chrony_dashboard.py:/opt/chrony-dashboard/chrony_dashboard.py \
      systemd/chrony-webpage.service:/etc/systemd/system/chrony-webpage.service \
      $1; do
    src=\${pair%%:*}; dst=\${pair#*:}
    if [ ! -e \"\$dst\" ]; then echo \"--- \$dst does not exist yet\"; continue; fi
    diff -u --label \"pi:\$dst\" --label \"repo:\$src\" \"\$dst\" \"\$src\" || true
  done"
}
CONFIG_PAIRS="etc/chrony/chrony.conf:/etc/chrony/chrony.conf etc/default/gpsd:/etc/default/gpsd boot/firmware/config.txt:/boot/firmware/config.txt"

if [[ $MODE == diff ]]; then
  remote_diff "$CONFIG_PAIRS"
  exit 0
fi

INSTALL_ARGS=()
if [[ $CONFIGS -eq 1 ]]; then
  log "Config changes that will be applied on the Pi:"
  cfg_diff="$("${SSH[@]}" "cd ~/$STAGE && for pair in $CONFIG_PAIRS; do
      src=\${pair%%:*}; dst=\${pair#*:}
      diff -u --label \"pi:\$dst\" --label \"repo:\$src\" \"\$dst\" \"\$src\" 2>&1 || true
    done")"
  if [[ -z "$cfg_diff" ]]; then
    echo "    (none - Pi already matches the repo)"
  else
    echo "$cfg_diff"
    if [[ $YES -ne 1 ]]; then
      read -r -p "Apply these config changes and restart chrony/gpsd? [y/N] " ans
      [[ $ans == [yY]* ]] || { echo "aborted"; exit 1; }
    fi
    INSTALL_ARGS+=(--configs)
  fi
fi

log "Installing on $PI_HOST"
if "${SSH[@]}" "sudo -n true" 2>/dev/null; then
  "${SSH[@]}" "sudo -n bash ~/$STAGE/install.sh ${INSTALL_ARGS[*]:-}"
else
  # sudo wants a password: needs a terminal.
  ssh -t -o ConnectTimeout=10 "$PI_HOST" "sudo bash ~/$STAGE/install.sh ${INSTALL_ARGS[*]:-}"
fi
log "Deployed $REV to $PI_HOST"
