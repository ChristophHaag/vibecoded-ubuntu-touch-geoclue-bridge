#!/bin/bash
# Set up (or repair) everything needed for GPS via the GeoClue2 bridge.
#
# Safe to re-run at any time. Each step only changes what's missing or
# outdated, and the bridge is only restarted if its code or unit changed.
# Run it after deploying new bridge files, after a system update and after
# a Firefox update.
#
#   1. GPS HIDL workaround (~/gps-hidl-workaround, if deployed)
#   2. D-Bus policy for org.freedesktop.GeoClue2 (on the read-only rootfs)
#   3. geoclue2-bridge systemd service (installed, enabled, running, current)
#   4. Firefox: 'location' policy group + GeoClue2 AppArmor rules
#
# Usage (ON THE DEVICE):
#   sudo ~/geoclue2-bridge/gps.sh [firefox_app_id]
#        ~/geoclue2-bridge/gps.sh --status        (no root needed)
# From the host, ./deploy.sh copies the files and runs this.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLICKS_DIR=/var/lib/apparmor/clicks
PROFILES_DIR=/var/lib/apparmor/profiles
DBUS_CONF=/etc/dbus-1/system.d/org.freedesktop.GeoClue2.conf
UNIT_NAME=geoclue2-bridge.service
UNIT=/etc/systemd/system/$UNIT_NAME
WORKAROUND=/home/phablet/gps-hidl-workaround/gps-hidl-workaround.sh
MARKER="# GeoClue2 bridge access"

errors=0
fail() { echo "  ERROR: $*"; errors=$((errors + 1)); }

detect_firefox() {
    ls "$CLICKS_DIR"/firefox.fuseteam_navigator_*.json 2>/dev/null |
        sort -V | tail -n 1 | xargs -r -n1 basename | sed 's/\.json$//'
}

file_md5() { md5sum "$1" 2>/dev/null | cut -d' ' -f1; }

has_location_group() {
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    sys.exit(0 if 'location' in json.load(f).get('policy_groups', []) else 1)
" "$CLICKS_DIR/$1.json" 2>/dev/null
}

show_status() {
    echo "=== Status ==="
    if [ -x "$WORKAROUND" ]; then
        "$WORKAROUND" status | sed 's/^/  workaround /'
    else
        echo "  workaround not deployed"
    fi
    if cmp -s "$SCRIPT_DIR/org.freedesktop.GeoClue2.conf" "$DBUS_CONF"; then
        echo "  D-Bus policy: current"
    else
        echo "  D-Bus policy: MISSING or outdated"
    fi
    echo "  bridge: $(systemctl is-enabled $UNIT_NAME 2>/dev/null || echo not-installed)" \
         "/ $(systemctl is-active $UNIT_NAME 2>/dev/null)," \
         "started $(systemctl show -p ActiveEnterTimestamp --value $UNIT_NAME)"
    if bridge_outdated; then
        echo "  bridge: code changed since start, restart needed"
    fi
    local app="${1:-$(detect_firefox)}"
    if [ -z "$app" ]; then
        echo "  firefox: not installed"
        return
    fi
    local loc=no rules
    has_location_group "$app" && loc=yes
    rules=$(grep -c "^  $MARKER" "$PROFILES_DIR/click_$app" 2>/dev/null)
    echo "  firefox: $app, location group: $loc, GeoClue2 AppArmor blocks: ${rules:-0} (want 1)"
}

# Checksum of the bridge code + unit the service was last (re)started with,
# plus the service start time it belongs to. Lets us tell real code changes
# from files that were merely copied again (scp resets mtimes).
STAMP=/run/geoclue2-bridge.deployed

bridge_sig() { cat "$SCRIPT_DIR/geoclue2-bridge.py" "$UNIT" 2>/dev/null | md5sum | cut -d' ' -f1; }
bridge_started() { systemctl show -p ActiveEnterTimestamp --value $UNIT_NAME; }

# True if the running bridge doesn't match the code/unit on disk.
bridge_outdated() {
    systemctl is-active -q $UNIT_NAME || return 1
    local ts sig stamp_sig stamp_ts started f
    ts=$(bridge_started)
    if [ -r "$STAMP" ]; then
        { read -r stamp_sig; read -r stamp_ts; } < "$STAMP"
        if [ "$stamp_ts" = "$ts" ]; then
            [ "$stamp_sig" != "$(bridge_sig)" ]
            return
        fi
    fi
    # No stamp for this service start (first run after boot or after a
    # manual restart): fall back to comparing mtimes with the start time.
    started=$(date -d "$ts" +%s 2>/dev/null) || return 0
    for f in "$SCRIPT_DIR/geoclue2-bridge.py" "$UNIT"; do
        [ "$(stat -c %Y "$f" 2>/dev/null || echo 0)" -gt "$started" ] && return 0
    done
    return 1
}

write_stamp() {
    printf '%s\n%s\n' "$(bridge_sig)" "$(bridge_started)" > "$STAMP"
}

if [ "${1:-}" = "--status" ]; then
    show_status "${2:-}"
    exit 0
fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
    exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo: sudo $0 [firefox_app_id]   (or $0 --status)"
    exit 1
fi

for f in geoclue2-bridge.py org.freedesktop.GeoClue2.conf geoclue2-bridge.service \
         add-location-policy.sh patch-apparmor-geoclue2.sh; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo "ERROR: $SCRIPT_DIR/$f missing. Run './deploy.sh' from the host."
        exit 1
    fi
done

echo "[1/4] GPS HIDL workaround"
if [ -x "$WORKAROUND" ]; then
    "$WORKAROUND" install 2>&1 | sed 's/^/  /'
else
    echo "  not deployed, skipping (only needed while the UT AIDL GNSS bug exists)"
fi

echo "[2/4] D-Bus policy"
if cmp -s "$SCRIPT_DIR/org.freedesktop.GeoClue2.conf" "$DBUS_CONF"; then
    echo "  up to date"
else
    remounted=no
    if findmnt -no OPTIONS / | grep -qw ro; then
        mount -o remount,rw / && remounted=yes
    fi
    if install -m 644 "$SCRIPT_DIR/org.freedesktop.GeoClue2.conf" "$DBUS_CONF"; then
        echo "  installed $DBUS_CONF"
        systemctl reload dbus || fail "dbus reload failed"
    else
        fail "could not install $DBUS_CONF"
    fi
    [ "$remounted" = yes ] && mount -o remount,ro /
fi

echo "[3/4] Bridge service"
restart=no
if ! cmp -s "$SCRIPT_DIR/geoclue2-bridge.service" "$UNIT"; then
    install -m 644 "$SCRIPT_DIR/geoclue2-bridge.service" "$UNIT" &&
        systemctl daemon-reload && echo "  installed $UNIT" && restart=yes
fi
systemctl is-enabled -q $UNIT_NAME || systemctl enable $UNIT_NAME
if ! systemctl is-active -q $UNIT_NAME; then
    echo "  starting"
    systemctl start $UNIT_NAME
elif [ "$restart" = yes ] || bridge_outdated; then
    echo "  restarting to load new code"
    systemctl restart $UNIT_NAME
else
    echo "  running, up to date"
fi
sleep 1
if systemctl is-active -q $UNIT_NAME; then
    write_stamp
else
    fail "bridge not running: journalctl -u $UNIT_NAME"
fi

echo "[4/4] Firefox permissions"
APP_ID="${1:-$(detect_firefox)}"
if [ -z "$APP_ID" ]; then
    echo "  Firefox not installed, skipping"
elif [ ! -f "$CLICKS_DIR/$APP_ID.json" ]; then
    fail "no click policy for $APP_ID"
else
    echo "  $APP_ID"
    profile="$PROFILES_DIR/click_$APP_ID"
    before=$(file_md5 "$profile")
    # add-location-policy.sh regenerates all click profiles with
    # aa-clickhook (dropping our GeoClue2 rules) if it has to add the group,
    # so the AppArmor patch must come after it.
    bash "$SCRIPT_DIR/add-location-policy.sh" "$APP_ID" 2>&1 |
        grep -v '^Updated:\|^  \|^{\|^}' | sed '/^$/d; s/^/  /'
    bash "$SCRIPT_DIR/patch-apparmor-geoclue2.sh" "$APP_ID" 2>&1 | sed 's/^/  /'
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "AppArmor patch failed"
    if [ "$(file_md5 "$profile")" != "$before" ]; then
        echo "  Profile changed: restart Firefox if it is running."
    fi
fi

echo
show_status "${1:-}"
echo
if [ "$errors" -gt 0 ]; then
    echo "=== $errors error(s) ==="
    exit 1
fi
echo "=== Done ==="
