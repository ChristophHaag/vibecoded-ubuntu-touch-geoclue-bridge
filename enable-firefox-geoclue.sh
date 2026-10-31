#!/bin/bash
# Grant GPS (GeoClue2 bridge) access to the currently installed Firefox.
#
# Runs both required steps in the correct order:
#   1. add-location-policy.sh  - adds the 'location' policy group (bridge
#      confinement check), then regenerates the AppArmor profile via
#      aa-clickhook (which wipes any previous GeoClue2 rules)
#   2. patch-apparmor-geoclue2.sh - adds the org.freedesktop.GeoClue2
#      D-Bus rules to the regenerated profile
#
# Lives in the geoclue2-bridge dir alongside the helper scripts it calls.
#
# Usage (ON THE DEVICE, use -t from host so sudo can prompt for password):
#   ssh -t phablet@192.168.1.120 "sudo ~/geoclue2-bridge/gps.sh"
#
# Optionally pass a specific app id to skip auto-detection:
#   sudo ~/geoclue2-bridge/gps.sh firefox.fuseteam_navigator_153.0.4

set -e

CLICKS_DIR="/var/lib/apparmor/clicks"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo: sudo $0 [app_id]"
    exit 1
fi

detect_firefox() {
    local matches
    matches=$(ls "$CLICKS_DIR"/firefox.fuseteam_navigator_*.json 2>/dev/null || true)
    if [ -z "$matches" ]; then
        echo "ERROR: No installed Firefox click app found in $CLICKS_DIR"
        exit 1
    fi
    # If multiple versions are installed, use the highest one
    echo "$matches" | sort -V | tail -n 1 | xargs -n1 basename | sed 's/\.json$//'
}

APP_ID="${1:-$(detect_firefox)}"

ADD_POLICY="$SCRIPT_DIR/add-location-policy.sh"
PATCH_AAP="$SCRIPT_DIR/patch-apparmor-geoclue2.sh"

for s in "$ADD_POLICY" "$PATCH_AAP"; do
    if [ ! -f "$s" ]; then
        echo "ERROR: Required script not found: $s"
        echo "Run './deploy.sh install' from the host to deploy the bridge files."
        exit 1
    fi
done

if [ ! -f "$CLICKS_DIR/${APP_ID}.json" ]; then
    echo "ERROR: No click policy found for app id: $APP_ID"
    echo "Installed Firefox apps:"
    ls "$CLICKS_DIR"/firefox.fuseteam_navigator_*.json 2>/dev/null | xargs -n1 basename | sed 's/\.json$//'
    exit 1
fi

echo "=== Enabling GeoClue2 bridge access for: $APP_ID ==="
echo ""

echo "[1/2] Adding 'location' policy group (regenerates AppArmor profile)..."
bash "$ADD_POLICY" "$APP_ID"
echo ""

echo "[2/2] Patching AppArmor profile with GeoClue2 D-Bus rules..."
bash "$PATCH_AAP" "$APP_ID"
echo ""

echo "=== Done ==="
echo "$APP_ID can now use GPS via the GeoClue2 bridge."
echo "If Firefox is running, restart it to pick up the new profile."
