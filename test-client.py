#!/usr/bin/env python3
"""GeoClue2 test client: Start, wait for a fix, Stop, then confirm the bridge survives.

Run ON THE DEVICE:  python3 test-client.py [fix_timeout_s] [post_stop_wait_s]
Exit code: 0 = got a fix, 2 = no fix (e.g. indoors), 1 = bridge died/error.
"""
import sys
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

GC = 'org.freedesktop.GeoClue2'
fix_timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 30
post_stop_wait = int(sys.argv[2]) if len(sys.argv) > 2 else 8

DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
owner = bus.get_name_owner(GC)
manager = dbus.Interface(bus.get_object(GC, '/org/freedesktop/GeoClue2/Manager'), f'{GC}.Manager')
client_path = manager.GetClient()
client_obj = bus.get_object(GC, client_path)
props = dbus.Interface(client_obj, 'org.freedesktop.DBus.Properties')
props.Set(f'{GC}.Client', 'DesktopId', 'test-client')
props.Set(f'{GC}.Client', 'RequestedAccuracyLevel', dbus.UInt32(8))

loop = GLib.MainLoop()
got_fix = False


def on_update(old, new):
    global got_fix
    p = dbus.Interface(bus.get_object(GC, new), 'org.freedesktop.DBus.Properties').GetAll(f'{GC}.Location')
    desc = str(p.get('Description', ''))
    print(f"{'CACHED' if desc else 'FIX'}: t={time.monotonic() - t_start:.1f}s "
          f"lat={float(p['Latitude']):.6f} lon={float(p['Longitude']):.6f} "
          f"acc={float(p['Accuracy']):.1f}m alt={float(p['Altitude']):.1f} {desc}")
    # A cached "last known" fix is reported but doesn't count as a GPS fix.
    if not desc:
        got_fix = True
        loop.quit()


client_obj.connect_to_signal('LocationUpdated', on_update, dbus_interface=f'{GC}.Client')
print(f'Client {client_path} on bridge {owner}; waiting up to {fix_timeout}s for a fix...')
t_start = time.monotonic()
dbus.Interface(client_obj, f'{GC}.Client').Start()
GLib.timeout_add_seconds(fix_timeout, loop.quit)
loop.run()
if not got_fix:
    print('No fix within timeout (no satellite visibility?)')

dbus.Interface(client_obj, f'{GC}.Client').Stop()
print(f'Stopped; waiting {post_stop_wait}s to check the bridge survives GPS shutdown...')
time.sleep(post_stop_wait)
try:
    new_owner = bus.get_name_owner(GC)
except dbus.DBusException:
    new_owner = None
if new_owner != owner:
    print(f'FAIL: bridge owner changed {owner} -> {new_owner} (crashed/restarted)')
    sys.exit(1)
print('OK: bridge still alive')
sys.exit(0 if got_fix else 2)
