#!/usr/bin/env python3
"""Off-device test of geoclue2-bridge.py against a fake lomiri-location-service.

Runs everything on a private session bus (no root, no phone needed):

    dbus-run-session -- python3 test-bridge-mock.py

Checks: async lomiri session setup, cached last fix after the grace period,
real fixes replacing it, heading 0.0, rejecting spoofed UpdatePosition calls,
per-peer client reuse and owner checks, fix cache persisted on SIGTERM, and
that a hung lomiri doesn't block the GeoClue2 API.
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import dbus
import dbus.lowlevel
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

HERE = os.path.dirname(os.path.abspath(__file__))
GC = 'org.freedesktop.GeoClue2'
GC_MANAGER = '/org/freedesktop/GeoClue2/Manager'
LOMIRI = 'com.lomiri.location.Service'
REAL_LAT, REAL_LON = 48.7747, 9.2776
CACHED_LAT, CACHED_LON = 1.0, 2.0
SPOOF_LAT = 66.6


def run_bridge(cache_file):
    spec = importlib.util.spec_from_file_location('bridge', os.path.join(HERE, 'geoclue2-bridge.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.dbus.SystemBus = dbus.SessionBus
    mod.LastFixCache.__init__.__defaults__ = (cache_file,)
    mod.CACHED_FIX_GRACE_SECONDS = 1
    mod.GPS_STOP_DELAY_SECONDS = 1
    mod.RECONNECT_DELAY_SECONDS = 1
    mod.LOMIRI_CALL_TIMEOUT_SECONDS = 2
    mod.ConfinementChecker.check_access = lambda self, sender: True
    mod.GeoClue2Bridge().run()


class FakeSession(dbus.service.Object):
    def __init__(self, bus, cb_bus, path, client, first_fix_delay):
        super().__init__(bus, path)
        # Like real lomiri: callbacks come from a second connection of the process.
        self._bus, self._path, self._client = cb_bus, path, client
        self._delay = first_fix_delay
        self._timer = None

    @dbus.service.method('com.lomiri.location.Service.Session')
    def StartPositionUpdates(self):
        self._call('UpdateHeading', [(0.0, 'd'), (time.time_ns(), 'x')])
        self._timer = GLib.timeout_add(int(self._delay * 1000), self._tick)

    @dbus.service.method('com.lomiri.location.Service.Session')
    def StartHeadingUpdates(self):
        pass

    @dbus.service.method('com.lomiri.location.Service.Session')
    def StopPositionUpdates(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    @dbus.service.method('com.lomiri.location.Service.Session')
    def StopHeadingUpdates(self):
        pass

    def _tick(self):
        self._call('UpdatePosition', [
            (REAL_LAT, 'd'), (REAL_LON, 'd'), (True, 'b'), (300.0, 'd'),
            (True, 'b'), (5.0, 'd'), (False, 'b'), (time.time_ns(), 'x')])
        self._timer = GLib.timeout_add(500, self._tick)
        return False

    def _call(self, member, args):
        msg = dbus.lowlevel.MethodCallMessage(self._client, self._path,
                                              'com.lomiri.location.Service.Session', member)
        for value, sig in args:
            msg.append(value, signature=sig)
        self._bus.send_message_with_reply(msg, lambda r: None, 5)


class FakeLomiri(dbus.service.Object):
    def __init__(self, bus, mode):
        super().__init__(bus, '/com/lomiri/location/Service')
        self._bus, self._mode, self._n = bus, mode, 0
        self._cb_bus = dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])

    @dbus.service.method(LOMIRI, in_signature='bbbbdbbb', out_signature='o',
                         sender_keyword='sender', async_callbacks=('ok', 'err'))
    def CreateSessionForCriteria(self, pos, alt, hdg, vel, acc, hv, hvel, hh,
                                 sender=None, ok=None, err=None):
        if self._mode == 'hang':
            return  # never reply
        self._n += 1
        path = f'/sessions/{self._n}'
        FakeSession(self._bus, self._cb_bus, path, sender, first_fix_delay=2.5)
        ok(dbus.ObjectPath(path))


def run_lomiri(mode):
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(LOMIRI, bus)  # noqa: F841
    FakeLomiri(bus, mode)
    GLib.MainLoop().run()


# --------------------------------------------------------------------------- driver

failures = []


def check(cond, what):
    print(('PASS ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)


def pump(seconds, until=None):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


def wait_name(bus, name, timeout=5):
    return pump(timeout, lambda: bus.name_has_owner(name))


def expect_denied(fn, what):
    try:
        fn()
        check(False, what)
    except dbus.DBusException as e:
        check(e.get_dbus_name() == 'org.freedesktop.DBus.Error.AccessDenied', what)


def spawn(*args):
    return subprocess.Popen([sys.executable, __file__, *args])


def driver():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    other = dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])
    tmp = tempfile.mkdtemp()
    cache = os.path.join(tmp, 'sub', 'last-fix.json')
    os.makedirs(os.path.dirname(cache))
    with open(cache, 'w') as f:
        json.dump({'latitude': CACHED_LAT, 'longitude': CACHED_LON, 'altitude': 10.0,
                   'horizontal_accuracy': 10.0, 'vertical_accuracy': None,
                   'timestamp': 0, 'heading': None,
                   'received_at': time.time() - 600}, f)

    procs = []
    try:
        # ---- normal operation
        procs.append(spawn('lomiri', 'normal'))
        procs.append(spawn('bridge', cache))
        check(wait_name(bus, LOMIRI) and wait_name(bus, GC), 'services up')

        mgr = dbus.Interface(bus.get_object(GC, GC_MANAGER), f'{GC}.Manager')
        c1 = mgr.GetClient()
        check(mgr.GetClient() == c1, 'GetClient reuses the per-peer client')
        check(mgr.CreateClient() != c1, 'CreateClient makes a new client')

        locations = []

        def on_updated(old, new, path=None):
            props = bus.get_object(GC, new).GetAll(f'{GC}.Location',
                                                    dbus_interface=dbus.PROPERTIES_IFACE)
            locations.append((str(new), dict(props)))

        bus.add_signal_receiver(on_updated, 'LocationUpdated', f'{GC}.Client', GC, c1)
        client = dbus.Interface(bus.get_object(GC, c1), f'{GC}.Client')
        expect_denied(lambda: other.get_object(GC, c1).Start(dbus_interface=f'{GC}.Client'),
                      'other peer cannot Start our client')

        t0 = time.monotonic()
        client.Start()
        check(pump(2.5, lambda: locations), 'cached fix arrives within grace period')
        if locations:
            p = locations[0][1]
            check(abs(p['Latitude'] - CACHED_LAT) < 1e-9, 'first location is the cached fix')
            check(p['Accuracy'] > 800, f"cached accuracy degraded by age ({p['Accuracy']:.0f} m)")
            check('Last known' in p['Description'], 'cached fix has a description')
            expect_denied(lambda: other.get_object(GC, locations[0][0]).GetAll(
                f'{GC}.Location', dbus_interface=dbus.PROPERTIES_IFACE),
                'other peer cannot read our Location')

        # Spoof an UpdatePosition at the bridge from a non-lomiri connection.
        bridge_unique = bus.get_name_owner(GC)
        for n in range(1, 3):
            msg = dbus.lowlevel.MethodCallMessage(bridge_unique, f'/sessions/{n}',
                                                  'com.lomiri.location.Service.Session',
                                                  'UpdatePosition')
            for value, sig in [(SPOOF_LAT, 'd'), (0.0, 'd'), (False, 'b'), (False, 'b'),
                               (False, 'b'), (time.time_ns(), 'x')]:
                msg.append(value, signature=sig)
            other.send_message(msg)

        check(pump(5, lambda: any(abs(l[1]['Latitude'] - REAL_LAT) < 1e-9 for l in locations)),
              f'real GPS fix arrives ({time.monotonic() - t0:.1f}s after Start)')
        pump(1)
        check(not any(abs(l[1]['Latitude'] - SPOOF_LAT) < 1e-9 for l in locations),
              'spoofed UpdatePosition ignored')
        real = [l[1] for l in locations if abs(l[1]['Latitude'] - REAL_LAT) < 1e-9]
        if real:
            check(real[-1]['Heading'] == 0.0, 'heading 0.0 (north) preserved')
            check(real[-1]['Accuracy'] == 5.0, 'real accuracy passed through')

        # A second peer starting while GPS runs gets the fresh fix immediately.
        c2 = dbus.Interface(other.get_object(GC, other.call_blocking(
            GC, GC_MANAGER, f'{GC}.Manager', 'GetClient', '', [])), f'{GC}.Client')
        got2 = []
        other.add_signal_receiver(lambda o, n: got2.append(n), 'LocationUpdated',
                                  f'{GC}.Client', GC, c2.object_path)
        c2.Start()
        check(pump(0.4, lambda: got2), 'late client gets current fix immediately')
        c2.Stop()

        client.Stop()
        pump(0.5)
        procs[1].send_signal(signal.SIGTERM)
        check(procs[1].wait(5) == 0, 'bridge exits cleanly on SIGTERM')
        with open(cache) as f:
            saved = json.load(f)
        check(abs(saved['latitude'] - REAL_LAT) < 1e-9, 'latest fix persisted to cache')
        check(oct(os.stat(cache).st_mode & 0o777) == '0o600', 'cache file is mode 600')
        procs[0].terminate()
        procs[0].wait(5)
        pump(0.5)

        # ---- hung lomiri must not block the GeoClue2 API
        procs = [spawn('lomiri', 'hang'), spawn('bridge', cache)]
        pump(5, lambda: bus.name_has_owner(LOMIRI) and bus.name_has_owner(GC) and
             bus.get_name_owner(GC) != bridge_unique)
        mgr = dbus.Interface(bus.get_object(GC, GC_MANAGER), f'{GC}.Manager')
        c3 = dbus.Interface(bus.get_object(GC, mgr.GetClient()), f'{GC}.Client')
        c3.Start()
        t = time.monotonic()
        mgr.GetAll(f'{GC}.Manager', dbus_interface=dbus.PROPERTIES_IFACE, timeout=1)
        pump(2.5)  # CreateSessionForCriteria times out (2 s) meanwhile
        mgr.GetAll(f'{GC}.Manager', dbus_interface=dbus.PROPERTIES_IFACE, timeout=1)
        check(time.monotonic() - t < 4, 'bridge stays responsive while lomiri hangs')
        c3.Stop()
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(5)

    print(f'\n{len(failures)} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'bridge':
        run_bridge(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == 'lomiri':
        run_lomiri(sys.argv[2])
    else:
        if 'DBUS_SESSION_BUS_ADDRESS' not in os.environ:
            sys.exit('Run under: dbus-run-session -- python3 test-bridge-mock.py')
        sys.exit(driver())
