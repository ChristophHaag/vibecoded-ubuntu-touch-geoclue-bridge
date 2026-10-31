#!/bin/bash
# GPS diagnostics: is the GNSS chip actually started, and does it see satellites?
#
# Runs a GeoClue2 session (via test-client.py) for N seconds and records:
#   - GPS IRQ deltas from /proc/interrupts (0 => chip never powered/started)
#   - lomiri-location-service visible_space_vehicles samples
#   - Android logcat lines from the MediaTek GNSS stack (HAL, mnld, lbs, agps)
#   - lomiri-location-service journal + kernel gps messages
#
# Usage (ON THE DEVICE, as root):
#   sudo ~/geoclue2-bridge/gps-diag.sh [seconds] [--restart-service|--restart-stack]
# --restart-service  restarts lomiri-location-service first
# --restart-stack    stops lomiri-location-service, restarts the Android GNSS
#                    stack (mnld, GNSS HAL, lbs_hidl_service) in the LXC
#                    container, then starts lomiri-location-service again
#
# Output is also written to /tmp/gps-diag.log (world-readable); logcat since
# the script started (unfiltered) goes to /tmp/gps-logcat-full.log.

DUR="${1:-30}"
RESTART="${2:-}"
OUT=/tmp/gps-diag.log
FULL_LOGCAT=/tmp/gps-logcat-full.log
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo: sudo $0 [seconds] [--restart-service|--restart-stack]"
    exit 1
fi

exec > >(tee "$OUT") 2>&1
chmod 644 "$OUT" 2>/dev/null

# Logcat timestamps are UTC (Android container timezone)
LC_START=$(date -u '+%m-%d %H:%M:%S')

android_sh() {
    lxc-attach -n android -- /system/bin/"$@"
}

logcat() {
    android_sh logcat "$@" 2>/dev/null || /system/bin/logcat "$@"
}

irq_counts() {
    grep -E 'gps' /proc/interrupts | awk '{s=0; for (i=2; i<=NF-3; i++) s+=$i; print $NF, s}'
}

svs() {
    lomiri-location-serviced-cli --bus system --property visible_space_vehicles --get 2>&1 \
        | grep -v "unregister path"
}

echo "=== GPS diagnostics $(date) (duration ${DUR}s) ==="

if [ "$RESTART" = "--restart-service" ]; then
    echo "--- Restarting lomiri-location-service"
    timeout 20 systemctl restart lomiri-location-service || systemctl kill -s KILL lomiri-location-service
    sleep 3
    systemctl is-active lomiri-location-service
elif [ "$RESTART" = "--restart-stack" ]; then
    echo "--- Stopping lomiri-location-service"
    timeout 20 systemctl stop lomiri-location-service || { systemctl kill -s KILL lomiri-location-service; sleep 1; }
    echo "--- Restarting Android GNSS stack (mnld, vendor.gnss-default, lbs_hidl_service)"
    for svc in mnld vendor.gnss-default lbs_hidl_service; do
        android_sh setprop ctl.restart "$svc"
        sleep 2
    done
    sleep 3
    echo "--- Starting lomiri-location-service"
    systemctl start lomiri-location-service
    sleep 3
    systemctl is-active lomiri-location-service
fi

echo "--- Processes"
ps -eo pid,user,lstart,args | grep -E 'gnss|mnld|agpsd|lbs_hidl|lomiri-location-serviced' | grep -v grep | cut -c1-160

IRQ0=$(mktemp)
irq_counts > "$IRQ0"
JSTART=$(date '+%Y-%m-%d %H:%M:%S')

echo "--- Starting GeoClue2 session as phablet"
sudo -u phablet python3 "$SCRIPT_DIR/test-client.py" "$DUR" 1 &
TC_PID=$!

for ((t = 5; t <= DUR; t += 5)); do
    sleep 5
    echo "--- t=${t}s $(svs | tr '\n' ' ')"
    irq_counts | awk '{printf "%s=%s ", $1, $2}'; echo
done
wait "$TC_PID"
echo "test-client exit code: $? (0=fix, 2=no fix, 1=bridge died)"

echo "--- GPS IRQ deltas during session (0 => chip not started)"
paste "$IRQ0" <(irq_counts) | awk '{print $1, $4 - $2}'
rm -f "$IRQ0"

echo "--- lomiri-location-service journal"
journalctl -u lomiri-location-service --since "$JSTART" --no-pager | grep -v "unregister path" | tail -30

echo "--- Kernel gps messages"
dmesg | grep -iE 'gps|gnss' | tail -20

echo "--- lomiri-location-serviced threads (state / wchan)"
LLS_PID=$(systemctl show -p MainPID --value lomiri-location-service)
if [ "${LLS_PID:-0}" -gt 0 ]; then
    for t in /proc/"$LLS_PID"/task/*; do
        echo "$(basename "$t") $(cat "$t"/comm) $(awk '{print $3}' "$t"/stat) $(cat "$t"/wchan 2>/dev/null)"
    done
fi

echo "--- Android logcat since script start (GNSS-related, last 200 lines)"
logcat -b all -d -v time | awk -v s="$LC_START" '($1 " " $2) >= s' > "$FULL_LOGCAT"
chmod 644 "$FULL_LOGCAT"
grep -iE 'gnss|gps|mnld|mnl|lbs|agps|location|nmea|ubuntu_application' "$FULL_LOGCAT" \
    | grep -v libmnlUtils | tail -200

echo "=== Done. Log: $OUT  Full logcat: $FULL_LOGCAT ==="
