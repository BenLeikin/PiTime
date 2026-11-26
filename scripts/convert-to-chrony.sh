#!/bin/bash
# convert-to-chrony.sh
# Switch a Linux host to chrony pointed at a specified time server with NTS.
# Edit TIME_SERVER below before running.
# Usage: bash convert-to-chrony.sh

set -e

TIME_SERVER="time.example.com"
FALLBACK_POOL="time.cloudflare.com"
LOG_PREFIX="[$(hostname)]"

log() { echo "$LOG_PREFIX $1"; }

if [ "$EUID" -ne 0 ]; then
    log "ERROR: must run as root"
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v yum >/dev/null 2>&1; then PKG="yum"
elif command -v pacman >/dev/null 2>&1; then PKG="pacman"
elif command -v zypper >/dev/null 2>&1; then PKG="zypper"
else
    log "ERROR: no supported package manager found"
    exit 1
fi

log "Detected package manager: $PKG"

HAS_CHRONY=0
systemctl list-unit-files 2>/dev/null | grep -q "^chrony.service\|^chronyd.service" && HAS_CHRONY=1

# Stop conflicting daemons
systemctl stop ntp ntpd ntpsec systemd-timesyncd openntpd 2>/dev/null || true
systemctl disable ntp ntpd ntpsec systemd-timesyncd openntpd 2>/dev/null || true

if [ $HAS_CHRONY -eq 0 ]; then
    log "Installing chrony"
    case "$PKG" in
        apt) DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony ;;
        dnf|yum) $PKG install -y chrony ;;
        pacman) pacman -Sy --noconfirm chrony ;;
        zypper) zypper install -y chrony ;;
    esac
fi

CHRONY_CONF=""
for path in /etc/chrony/chrony.conf /etc/chrony.conf; do
    [ -f "$path" ] && CHRONY_CONF="$path" && break
done

if [ -z "$CHRONY_CONF" ]; then
    log "ERROR: chrony.conf not found"
    exit 1
fi

BACKUP="${CHRONY_CONF}.bak.$(date +%Y%m%d-%H%M%S)"
cp "$CHRONY_CONF" "$BACKUP"
log "Backup saved to $BACKUP"

sed -i 's|^[[:space:]]*pool |#pool |g; s|^[[:space:]]*server |#server |g' "$CHRONY_CONF"
sed -i '/^# === managed by convert-to-chrony.sh ===$/,/^# === end managed block ===$/d' "$CHRONY_CONF"

cat >> "$CHRONY_CONF" <<EOF

# === managed by convert-to-chrony.sh ===
server $TIME_SERVER iburst nts prefer
pool $FALLBACK_POOL iburst nts maxsources 2
makestep 1.0 3
rtcsync
# === end managed block ===
EOF

CHRONY_SVC="chrony"
systemctl list-unit-files 2>/dev/null | grep -q "^chronyd.service" && CHRONY_SVC="chronyd"

systemctl enable "$CHRONY_SVC" >/dev/null 2>&1
systemctl restart "$CHRONY_SVC"
sleep 5

if chronyc tracking >/dev/null 2>&1; then
    log "OK"
    chronyc sources -n
else
    log "ERROR: chronyc tracking failed"
    exit 1
fi
