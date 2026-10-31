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
#   ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh <app_id>"
#
# Safe to re-run: an existing block is replaced, not duplicated.

set -e

PROFILES_DIR="/var/lib/apparmor/profiles"
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

    # Strip any previous copies of our block (older versions of this script
    # failed to remove them and appended a duplicate on every run), then
    # append exactly one block before the profile's closing brace.
    MARKER="$MARKER" python3 - "$profile" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
marker = os.environ['MARKER']
rules = '''  {marker}
  dbus (send)
       bus=system
       path="/org/freedesktop/GeoClue2{{,/**}}"
       interface="org.freedesktop.GeoClue2.Manager"
       peer=(label=unconfined),
  dbus (send)
       bus=system
       path="/org/freedesktop/GeoClue2{{,/**}}"
       interface="org.freedesktop.GeoClue2.Client"
       peer=(label=unconfined),
  dbus (send)
       bus=system
       path="/org/freedesktop/GeoClue2{{,/**}}"
       interface="org.freedesktop.DBus.Properties"
       peer=(label=unconfined),
  dbus (receive)
       bus=system
       path="/org/freedesktop/GeoClue2{{,/**}}"
       interface="org.freedesktop.GeoClue2.Client"
       peer=(label=unconfined),
'''.format(marker=marker)

with open(path) as f:
    old = f.read()
block_re = re.compile(r'^  ' + re.escape(marker) + r'\n(?:(?:  dbus |       ).*\n)*',
                      re.MULTILINE)
content = block_re.sub('', old)
idx = content.rfind('}')
if idx == -1:
    sys.exit('ERROR: No closing } found in profile')
content = content[:idx].rstrip('\n') + '\n\n' + rules + content[idx:]
if content == old:
    print('Already patched')
    sys.exit(0)
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    f.write(content)
os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)
print('Patched (%d old block(s) replaced)' % len(block_re.findall(old)))
PYEOF
    return 0
}

reload_profile() {
    local profile="$PROFILES_DIR/click_$1"
    echo "Reloading AppArmor profile..."
    apparmor_parser -r --write-cache "$profile"
}

# --- Main ---

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run with sudo."
    echo "  sudo $0 [app_id]"
    exit 1
fi

APP_ID="${1:-}"

if [ -z "$APP_ID" ] || [ "$APP_ID" = "--help" ] || [ "$APP_ID" = "-h" ]; then
    echo "Usage: sudo $0 <app_id>"
    echo ""
    echo "Patches an app's AppArmor profile to allow GeoClue2 D-Bus access."
    echo ""
    echo "Must be re-run after app updates or aa-clickhook --force."
    echo ""
    echo "From host:"
    echo "  ssh -t phablet@192.168.1.120 \"sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh <app_id>\""
    exit 0
fi

patch_profile "$APP_ID"
reload_profile "$APP_ID"
