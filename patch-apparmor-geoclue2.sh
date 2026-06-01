#!/bin/bash
# Patch an app's AppArmor profile to allow GeoClue2 D-Bus access.
#
# The Ubuntu Touch 'location' policy group only grants access to
# com.lomiri.location.Service, not org.freedesktop.GeoClue2.
# This script adds the missing GeoClue2 rules to a click app's profile.
#
# Must be re-run after:
#   - The app is updated (click install regenerates the profile)
#   - aa-clickhook --force is run
#
# Usage (ON THE DEVICE via SSH with -t for sudo):
#   ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh"
#   ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh <app_id>"
#
# Without arguments, patches Firefox. With an argument, patches the specified app.

set -e

PROFILES_DIR="/var/lib/apparmor/profiles"
DEFAULT_APP="firefox.fuseteam_navigator_150.0.2"
MARKER="# GeoClue2 bridge access (added by patch-apparmor-geoclue2.sh)"

patch_profile() {
    local app_id="$1"
    local profile="$PROFILES_DIR/click_${app_id}"

    if [ ! -f "$profile" ]; then
        echo "ERROR: Profile not found: $profile"
        echo ""
        echo "Available profiles:"
        ls "$PROFILES_DIR"/click_* 2>/dev/null | sed 's|.*/click_||' | head -20
        return 1
    fi

    # Remove old patch if present (allows re-patching with updated rules)
    if grep -q "$MARKER" "$profile" 2>/dev/null; then
        python3 -c "
import re
with open('$profile', 'r') as f:
    content = f.read()
# Remove from marker to end of the block (next empty line or closing brace)
pattern = r'\n  $MARKER.*?(?=\n  #|\n})'
content = re.sub(pattern, '', content, flags=re.DOTALL)
with open('$profile', 'w') as f:
    f.write(content)
"
        echo "Removed old patch, re-applying..."
    fi

    # Insert GeoClue2 rules before the closing }
    python3 -c "
import sys
rules = '''
  $MARKER
  dbus (send)
       bus=system
       path=\"/org/freedesktop/GeoClue2{,/**}\"
       interface=\"org.freedesktop.GeoClue2.Manager\"
       peer=(label=unconfined),
  dbus (send)
       bus=system
       path=\"/org/freedesktop/GeoClue2{,/**}\"
       interface=\"org.freedesktop.GeoClue2.Client\"
       peer=(label=unconfined),
  dbus (send)
       bus=system
       path=\"/org/freedesktop/GeoClue2{,/**}\"
       interface=\"org.freedesktop.DBus.Properties\"
       peer=(label=unconfined),
  dbus (receive)
       bus=system
       path=\"/org/freedesktop/GeoClue2{,/**}\"
       interface=\"org.freedesktop.GeoClue2.Client\"
       peer=(label=unconfined),
'''

with open('$profile', 'r') as f:
    content = f.read()

# Insert before the final }
idx = content.rfind('}')
if idx == -1:
    print('ERROR: No closing } found in profile', file=sys.stderr)
    sys.exit(1)

content = content[:idx] + rules + '\n' + content[idx:]

with open('$profile', 'w') as f:
    f.write(content)
"

    echo "Patched: $app_id"
    return 0
}

reload_profiles() {
    echo "Reloading AppArmor profiles..."
    if command -v apparmor_parser >/dev/null 2>&1; then
        apparmor_parser -r "$PROFILES_DIR"/click_* 2>/dev/null || true
    fi
    echo "Done."
}

# --- Main ---

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run with sudo."
    echo "  sudo $0 [app_id]"
    exit 1
fi

APP_ID="${1:-$DEFAULT_APP}"

if [ "$APP_ID" = "--help" ] || [ "$APP_ID" = "-h" ]; then
    echo "Usage: sudo $0 [app_id]"
    echo ""
    echo "Patches an app's AppArmor profile to allow GeoClue2 D-Bus access."
    echo "Default app: $DEFAULT_APP"
    echo ""
    echo "Must be re-run after app updates or aa-clickhook --force."
    echo ""
    echo "From host:"
    echo "  ssh -t phablet@192.168.1.120 \"sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh\""
    exit 0
fi

patch_profile "$APP_ID"
reload_profiles
echo ""
echo "Firefox needs to be restarted to pick up the new profile."
