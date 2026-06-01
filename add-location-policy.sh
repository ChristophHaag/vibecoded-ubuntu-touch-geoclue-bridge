#!/bin/bash
# Add or remove the 'location' policy group from a click app's AppArmor profile.
#
# This modifies the app's click AppArmor JSON and regenerates its profile,
# granting it access to location services (including the GeoClue2 bridge).
#
# Usage:
#   sudo ./add-location-policy.sh <app_id>          # Add location permission
#   sudo ./add-location-policy.sh --remove <app_id> # Remove location permission
#   sudo ./add-location-policy.sh --list            # List apps and their location status
#
# Examples:
#   sudo ./add-location-policy.sh firefox.fuseteam_navigator_150.0.2
#   sudo ./add-location-policy.sh --remove firefox.fuseteam_navigator_150.0.2
#
# This script must be run ON THE DEVICE (via SSH), not on the host.
# --list works without sudo; add/remove require sudo.
#
# From host (use -t so sudo can prompt for password):
#   ssh phablet@192.168.1.120 "bash /home/phablet/geoclue2-bridge/add-location-policy.sh --list"
#   ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/add-location-policy.sh firefox.fuseteam_navigator_150.0.2"

set -e

CLICKS_DIR="/var/lib/apparmor/clicks"

list_apps() {
    echo "=== Click Apps Location Policy Status ==="
    echo ""
    printf "%-55s %s\n" "APP ID" "LOCATION"
    printf "%-55s %s\n" "------" "--------"
    for f in "$CLICKS_DIR"/*.json; do
        [ -f "$f" ] || continue
        app_id=$(basename "$f" .json)
        if python3 -c "
import json, sys
with open('$f') as fh:
    p = json.load(fh)
sys.exit(0 if 'location' in p.get('policy_groups', []) else 1)
" 2>/dev/null; then
            printf "%-55s %s\n" "$app_id" "YES"
        else
            printf "%-55s %s\n" "$app_id" "no"
        fi
    done
}

add_location() {
    local app_id="$1"
    local json_file="$CLICKS_DIR/${app_id}.json"

    if [ ! -f "$json_file" ]; then
        echo "ERROR: No click policy found at $json_file"
        echo ""
        echo "Available apps:"
        ls "$CLICKS_DIR"/*.json 2>/dev/null | xargs -I{} basename {} .json | head -20
        exit 1
    fi

    # Check if already has location
    if python3 -c "
import json
with open('$json_file') as f:
    p = json.load(f)
if 'location' in p.get('policy_groups', []):
    exit(0)
exit(1)
" 2>/dev/null; then
        echo "App '$app_id' already has the location policy group."
        return
    fi

    # Backup original (NOT in clicks dir — aa-clickhook would try to parse it)
    local backup_dir="/home/phablet/.local/share/click-policy-backups"
    mkdir -p "$backup_dir"
    cp "$json_file" "$backup_dir/${app_id}.json.bak"
    echo "Backup saved to $backup_dir/${app_id}.json.bak"

    # Add location to policy_groups
    python3 -c "
import json
with open('$json_file', 'r') as f:
    p = json.load(f)
groups = p.get('policy_groups', [])
if 'location' not in groups:
    groups.append('location')
    p['policy_groups'] = groups
with open('$json_file', 'w') as f:
    json.dump(p, f, indent=4)
print('Updated:', json.dumps(p, indent=2))
"
    echo ""
    echo "Regenerating AppArmor profile..."
    aa-clickhook --force
    echo ""
    echo "SUCCESS: '$app_id' now has the location policy group."
    echo "The app can now access GPS via the GeoClue2 bridge."
}

remove_location() {
    local app_id="$1"
    local json_file="$CLICKS_DIR/${app_id}.json"

    if [ ! -f "$json_file" ]; then
        echo "ERROR: No click policy found at $json_file"
        exit 1
    fi

    # Remove location from policy_groups
    python3 -c "
import json
with open('$json_file', 'r') as f:
    p = json.load(f)
groups = p.get('policy_groups', [])
if 'location' in groups:
    groups.remove('location')
    p['policy_groups'] = groups
    with open('$json_file', 'w') as f:
        json.dump(p, f, indent=4)
    print('Removed location. Updated:', json.dumps(p, indent=2))
else:
    print('App does not have location permission (nothing to remove)')
"
    echo ""
    echo "Regenerating AppArmor profile..."
    aa-clickhook --force
    echo ""
    echo "Done: location permission removed from '$app_id'."
}

# Parse arguments
case "${1:-}" in
    --list|-l)
        list_apps
        ;;
    --remove|-r)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 --remove <app_id>"
            exit 1
        fi
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: This script must be run with sudo"
            exit 1
        fi
        remove_location "$2"
        ;;
    --help|-h)
        echo "Usage (run ON THE DEVICE, not host):"
        echo "  $0 --list                  List apps and status (no sudo needed)"
        echo "  sudo $0 <app_id>           Add location permission"
        echo "  sudo $0 --remove <app_id>  Remove location permission"
        echo ""
        echo "From host (use -t for sudo password prompt):"
        echo "  ssh phablet@192.168.1.120 \"bash /home/phablet/geoclue2-bridge/add-location-policy.sh --list\""
        echo "  ssh -t phablet@192.168.1.120 \"sudo /home/phablet/geoclue2-bridge/add-location-policy.sh <app_id>\""
        echo ""
        echo "Common app IDs:"
        echo "  firefox.fuseteam_navigator_150.0.2"
        echo "  uwolf.chromiumos-guy_uwolf_0.11.38"
        ;;
    "")
        echo "Usage: sudo $0 <app_id>"
        echo "Run '$0 --list' to see available apps."
        exit 1
        ;;
    *)
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: This script must be run with sudo"
            exit 1
        fi
        add_location "$1"
        ;;
esac
