#!/usr/bin/env bash
# Install or update openTimeServer on the Pi. Run as root from a checkout
# (or from the staging dir that deploy.sh uploads).
#
#   sudo ./install.sh              # dashboard + its systemd unit only
#   sudo ./install.sh --configs    # also chrony.conf, gpsd defaults, config.txt
#
# Config files that would change are backed up next to the original as
# <file>.bak-<timestamp> before being replaced.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/chrony-dashboard
UNIT=chrony-webpage.service
PORT=8080
WITH_CONFIGS=0

for arg in "$@"; do
  case "$arg" in
    --configs) WITH_CONFIGS=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must run as root (use sudo)" >&2
  exit 1
fi

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
STAMP="$(date +%Y%m%d-%H%M%S)"

# install_config <repo path> <system path> [mode]: copy only if different, keep a backup.
install_config() {
  local src="$SRC/$1" dst="$2" mode="${3:-0644}"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    echo "    unchanged: $dst"
    return 1
  fi
  if [[ -f "$dst" ]]; then
    local bak="$dst.bak-$STAMP"
    # NetworkManager would run a backup left in dispatcher.d as a script.
    [[ "$dst" == */dispatcher.d/* ]] && bak="/root/$(basename "$dst").bak-$STAMP"
    cp -a "$dst" "$bak"
    echo "    backup:    $bak"
  fi
  install -D -m "$mode" "$src" "$dst"
  echo "    updated:   $dst"
  return 0
}

if ! id _chrony >/dev/null 2>&1; then
  echo "user _chrony not found - is chrony installed?" >&2
  exit 1
fi

# Sanity-check the code before touching the running service.
# compile() checks syntax without writing a root-owned __pycache__ into the
# upload folder, which would block the next deploy.
python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' \
  "$SRC/dashboard/chrony_dashboard.py"

log "Installing dashboard to $APP_DIR"
install -d -m 0755 "$APP_DIR"
install -m 0644 "$SRC/dashboard/chrony_dashboard.py" "$APP_DIR/chrony_dashboard.py"
rm -rf "$APP_DIR/__pycache__"
install -m 0644 "$SRC/systemd/$UNIT" "/etc/systemd/system/$UNIT"

if [[ $WITH_CONFIGS -eq 1 ]]; then
  log "Installing system configs"
  if install_config etc/chrony/chrony.conf /etc/chrony/chrony.conf; then
    RESTART_CHRONY=1
  fi
  if install_config etc/default/gpsd /etc/default/gpsd; then
    RESTART_GPSD=1
  fi
  install_config etc/NetworkManager/dispatcher.d/50-link-failover-garp \
    /etc/NetworkManager/dispatcher.d/50-link-failover-garp 0755 || true
  if install_config boot/firmware/config.txt /boot/firmware/config.txt; then
    echo "    NOTE: config.txt changed - reboot the Pi for it to take effect"
  fi
  if [[ ${RESTART_GPSD:-0} -eq 1 ]]; then
    log "Restarting gpsd"
    systemctl restart gpsd.socket gpsd.service 2>/dev/null || systemctl restart gpsd
  fi
  if [[ ${RESTART_CHRONY:-0} -eq 1 ]]; then
    log "Restarting chrony"
    systemctl restart chrony
  fi
fi

log "Restarting $UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"

log "Waiting for the dashboard to answer"
for _ in $(seq 1 20); do
  if out="$(python3 - "$PORT" <<'EOF' 2>/dev/null
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=5) as r:
    print(json.dumps(json.load(r)))
EOF
)"; then
    echo "    health: $out"
    systemctl --no-pager --lines=0 status "$UNIT" | head -3
    log "Done"
    exit 0
  fi
  sleep 1
done

echo "dashboard did not come up; recent log:" >&2
journalctl -u "$UNIT" -n 30 --no-pager >&2
exit 1
