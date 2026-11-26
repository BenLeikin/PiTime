#!/usr/bin/env bash
# Install or update openTimeServer on the Pi. Run as root from a checkout
# (or from the staging dir that deploy.sh uploads).
#
#   sudo ./install.sh              # dashboard + its systemd unit only
#   sudo ./install.sh --configs    # also chrony, gpsd, boot and tuning configs
#
# Settings that differ from one site to the next (who may use the server, the
# NTS host name, the reverse proxy's address) live under site/ in this
# repository; edit those for your own server.
#
# Config files that would change are backed up next to the original as
# <file>.bak-<timestamp> before being replaced.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/chrony-dashboard
UNIT=chrony-webpage.service
PORT=8080
BOOT_CONFIG=/boot/firmware/config.txt
WITH_CONFIGS=0
RESTART_CHRONY=0
RESTART_GPSD=0

for arg in "$@"; do
  case "$arg" in
    --configs) WITH_CONFIGS=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must run as root (use sudo)" >&2
  exit 1
fi

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
STAMP="$(date +%Y%m%d-%H%M%S)"

# install_config <repo path> <system path> [mode]: copy only if different,
# keep a backup. Returns 0 if the file was installed, 1 if it was unchanged.
install_config() {
  local src="$SRC/$1" dst="$2" mode="${3:-0644}"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    echo "    unchanged: $dst"
    return 1
  fi
  if [[ -f "$dst" ]]; then
    local bak="$dst.bak-$STAMP"
    # Some directories run or load every file in them, backups included
    # (NetworkManager's dispatcher.d, chrony's conf.d), so keep those elsewhere.
    case "$dst" in
      */dispatcher.d/*|*/conf.d/*) bak="/root/$(basename "$dst").bak-$STAMP" ;;
    esac
    cp -a "$dst" "$bak"
    echo "    backup:    $bak"
  fi
  install -D -m "$mode" "$src" "$dst"
  echo "    updated:   $dst"
  return 0
}

# Add the lines listed in boot/firmware/config.txt.required that the boot
# config does not have yet. Nothing else in the file is touched.
ensure_boot_config() {
  local missing=() pattern line
  if [[ ! -f "$BOOT_CONFIG" ]]; then
    echo "    skipped:   $BOOT_CONFIG not found (not Raspberry Pi OS Bookworm?)"
    return 1
  fi
  while IFS=$'\t' read -r pattern line; do
    [[ -z "$pattern" || "$pattern" == \#* ]] && continue
    grep -qE "$pattern" "$BOOT_CONFIG" || missing+=("$line")
  done < "$SRC/boot/firmware/config.txt.required"
  if [[ ${#missing[@]} -eq 0 ]]; then
    echo "    unchanged: $BOOT_CONFIG"
    return 1
  fi
  cp -a "$BOOT_CONFIG" "$BOOT_CONFIG.bak-$STAMP"
  echo "    backup:    $BOOT_CONFIG.bak-$STAMP"
  {
    echo
    echo "# Added by openTimeServer install.sh"
    echo "[all]"
    printf '%s\n' "${missing[@]}"
  } >> "$BOOT_CONFIG"
  printf '    added to %s: %s\n' "$BOOT_CONFIG" "${missing[*]}"
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

if [[ $WITH_CONFIGS -eq 1 ]]; then
  # A chrony config that does not parse would take the time server down, so
  # have chronyd read the new files before anything is replaced.
  for conf in etc/chrony/chrony.conf site/etc/chrony/conf.d/site.conf; do
    if ! /usr/sbin/chronyd -p -f "$SRC/$conf" >/dev/null; then
      echo "$conf is not a valid chrony config - nothing was changed" >&2
      exit 1
    fi
  done
fi

log "Installing dashboard to $APP_DIR"
install -d -m 0755 "$APP_DIR" "$APP_DIR/static"
install -m 0644 "$SRC/dashboard/chrony_dashboard.py" "$APP_DIR/chrony_dashboard.py"
install -m 0644 "$SRC"/dashboard/static/* "$APP_DIR/static/"
rm -rf "$APP_DIR/__pycache__"
install -m 0644 "$SRC/systemd/$UNIT" "/etc/systemd/system/$UNIT"
# The unit reads the dashboard's site options from here.
install_config site/etc/default/chrony-dashboard /etc/default/chrony-dashboard || true

if [[ $WITH_CONFIGS -eq 1 ]]; then
  log "Installing system configs"
  if install_config etc/chrony/chrony.conf /etc/chrony/chrony.conf; then
    RESTART_CHRONY=1
  fi
  if install_config site/etc/chrony/conf.d/site.conf /etc/chrony/conf.d/site.conf; then
    RESTART_CHRONY=1
  fi
  if install_config etc/default/gpsd /etc/default/gpsd; then
    RESTART_GPSD=1
  fi
  install_config etc/NetworkManager/dispatcher.d/50-link-failover-garp \
    /etc/NetworkManager/dispatcher.d/50-link-failover-garp 0755 || true
  if install_config etc/sysctl.d/90-timeserver-failover.conf \
      /etc/sysctl.d/90-timeserver-failover.conf; then
    /usr/sbin/sysctl -q -p /etc/sysctl.d/90-timeserver-failover.conf
  fi
  # Boot-time tuning: always (re)run it so a changed script takes effect now.
  install_config usr/local/sbin/timeserver-tune /usr/local/sbin/timeserver-tune 0755 || true
  install_config systemd/timeserver-tune.service \
    /etc/systemd/system/timeserver-tune.service || true
  systemctl daemon-reload
  systemctl enable timeserver-tune.service >/dev/null 2>&1
  # Only where a GitLab runner is installed on this machine: keep its jobs
  # out of the way of timekeeping.
  if systemctl cat gitlab-runner.service >/dev/null 2>&1; then
    if install_config site/etc/systemd/system/gitlab-runner.service.d/timeserver.conf \
        /etc/systemd/system/gitlab-runner.service.d/timeserver.conf; then
      systemctl daemon-reload
      systemctl try-restart gitlab-runner.service
    fi
  fi
  if ensure_boot_config; then
    echo "    NOTE: $BOOT_CONFIG changed - reboot the Pi for it to take effect"
  fi
  if [[ $RESTART_GPSD -eq 1 ]]; then
    log "Restarting gpsd"
    systemctl restart gpsd.socket gpsd.service 2>/dev/null || systemctl restart gpsd
  fi
  if [[ $RESTART_CHRONY -eq 1 ]]; then
    log "Restarting chrony"
    systemctl restart chrony
  fi
  log "Applying low-jitter tuning"
  # After gpsd's restart above, so the GPS mode is sent to the running gpsd.
  systemctl restart timeserver-tune.service \
    || echo "    WARNING: tuning did not fully apply (journalctl -u timeserver-tune)"
  journalctl -u timeserver-tune.service -n 4 --no-pager -o cat | sed 's/^/    /'
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
