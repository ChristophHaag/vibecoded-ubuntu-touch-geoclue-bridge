#!/usr/bin/env python3
"""
GeoClue2 Bridge for Ubuntu Touch (Lomiri)

Provides a GeoClue2-compatible D-Bus service (org.freedesktop.GeoClue2) that
bridges to lomiri-location-service, enabling Firefox/LibreWolf and other
desktop Linux applications to access GPS on Ubuntu Touch.

The lomiri-location-service uses a callback protocol:
  1. Client calls CreateSessionForCriteria on the service
  2. Service calls UpdatePosition/UpdateHeading/UpdateVelocity methods
     back on the CLIENT's D-Bus object at the session path

This bridge:
  - Exposes org.freedesktop.GeoClue2 on the system bus
  - Implements Manager, Client, and Location interfaces
  - Connects to lomiri-location-service as a location client
  - Forwards position data from lomiri to GeoClue2 consumers
  - Enforces Ubuntu Touch confinement: only apps with the 'location'
    policy group (or unconfined apps) can access location data
  - GPS lifecycle: only activates GPS when clients are actively requesting
    position updates; shuts down when no clients need it
  - Fault tolerance: reconnects to lomiri if it crashes, cleans up
    disconnected clients, reloads whitelist on SIGHUP; lomiri calls are
    asynchronous so a hung lomiri never blocks GeoClue2 clients
  - Last known fix: cached (and persisted to ~/.cache) so clients get a
    position quickly while GPS is still acquiring
"""

import glob
import json
import os
import signal
import sys
import time

import logging
from typing import Optional, Dict, Any, Set

import dbus
import dbus.service
import dbus.lowlevel
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# Single-threaded: all callbacks run on the GLib main loop.
# No threading is used; do not add threads without adding locking.

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
log = logging.getLogger('geoclue2-bridge')

# GeoClue2 accuracy levels
GCLUE_ACCURACY_LEVEL_NONE = 0
GCLUE_ACCURACY_LEVEL_COUNTRY = 1
GCLUE_ACCURACY_LEVEL_CITY = 4
GCLUE_ACCURACY_LEVEL_NEIGHBORHOOD = 5
GCLUE_ACCURACY_LEVEL_STREET = 6
GCLUE_ACCURACY_LEVEL_EXACT = 8

# Lomiri location service constants
LOMIRI_SERVICE_NAME = 'com.lomiri.location.Service'
LOMIRI_SERVICE_PATH = '/com/lomiri/location/Service'
LOMIRI_SERVICE_IFACE = 'com.lomiri.location.Service'
LOMIRI_SESSION_IFACE = 'com.lomiri.location.Service.Session'

GEOCLUE2_BUS_NAME = 'org.freedesktop.GeoClue2'

# AppArmor click policy directory
CLICK_APPARMOR_DIR = '/var/lib/apparmor/clicks'

# Optional whitelist file: one app_id per line (e.g., "firefox.fuseteam_navigator_150.0.2")
# Apps listed here are allowed even without the 'location' policy group.
WHITELIST_FILE = '/home/phablet/geoclue2-bridge/location-whitelist.txt'

# Delay before stopping GPS after last client stops (allows for quick re-requests)
GPS_STOP_DELAY_SECONDS = 5

# Delay before attempting to reconnect to lomiri after failure
RECONNECT_DELAY_SECONDS = 10

# Timeout for calls to lomiri-location-service (all asynchronous)
LOMIRI_CALL_TIMEOUT_SECONDS = 10

# "Unknown" values of the GeoClue2 Location interface
GCLUE_ALTITUDE_UNKNOWN = -sys.float_info.max
GCLUE_UNKNOWN = -1.0  # Speed, Heading
# lomiri normally always reports accuracy; never claim 0 m if it doesn't
UNKNOWN_ACCURACY_M = 1000.0

# Last known fix. Firefox falls back to network geolocation when GeoClue sends
# nothing within 5 s of Start() (geo.provider.geoclue.mls_fallback_timeout_ms),
# and web apps commonly use 5-10 s getCurrentPosition() timeouts. So if GPS has
# no fix CACHED_FIX_GRACE_SECONDS after Start(), clients get the last known fix.
CACHE_FILE = os.path.join(os.path.expanduser('~'), '.cache', 'geoclue2-bridge',
                          'last-fix.json')
CACHED_FIX_GRACE_SECONDS = 3
CACHED_FIX_MAX_AGE_SECONDS = 3600
# Firefox stamps every GeoClue location with the current time, so a stale fix
# can only be marked as such through its accuracy: grow it at walking speed.
CACHED_FIX_DRIFT_M_PER_S = 1.5
# A fix younger than this is sent to newly started clients immediately.
FRESH_FIX_SECONDS = 5
CACHE_SAVE_INTERVAL_SECONDS = 600


class ConfinementChecker:
    """Checks whether a D-Bus caller is allowed to access location data.

    Access is granted if:
    1. The caller is 'unconfined' (system services, unconfined click apps), OR
    2. The caller's click app has 'location' in its policy_groups, OR
    3. The caller's app_id is in the whitelist file

    Reload whitelist by calling reload_whitelist() (e.g., on SIGHUP).
    """

    def __init__(self, bus: dbus.SystemBus):
        self._bus = bus
        self._whitelist: Set[str] = set()
        self._load_whitelist()

    def reload_whitelist(self):
        """Reload whitelist from disk. Safe to call at any time."""
        old_size = len(self._whitelist)
        self._load_whitelist()
        log.info("Whitelist reloaded: %d entries (was %d)", len(self._whitelist), old_size)

    def _load_whitelist(self):
        """Load app whitelist from file."""
        try:
            if os.path.exists(WHITELIST_FILE):
                with open(WHITELIST_FILE, 'r') as f:
                    self._whitelist = {
                        line.strip() for line in f
                        if line.strip() and not line.startswith('#')
                    }
                if self._whitelist:
                    log.info("Loaded whitelist: %s", self._whitelist)
            else:
                self._whitelist = set()
        except Exception as e:
            log.warning("Could not load whitelist: %s", e)

    def check_access(self, sender: str) -> bool:
        """Check if the D-Bus sender is allowed to access location.

        Args:
            sender: The unique bus name of the caller (e.g., ':1.42')

        Returns:
            True if access should be granted, False otherwise.
        """
        try:
            label = self._get_apparmor_label(sender)
        except Exception as e:
            log.warning("Could not get AppArmor label for %s: %s", sender, e)
            return False

        # Unconfined processes are always allowed
        if label == 'unconfined':
            log.debug("Access granted (unconfined) for %s", sender)
            return True

        # Parse app_id from label (format: "app_id (enforce)" or "app_id (complain)")
        app_id = label.split(' ')[0] if ' ' in label else label

        # Check whitelist
        if app_id in self._whitelist:
            log.info("Access granted (whitelist) for %s [%s]", sender, app_id)
            return True

        # Check click app's policy_groups
        if self._has_location_policy(app_id):
            log.info("Access granted (location policy) for %s [%s]", sender, app_id)
            return True

        log.warning("Access DENIED for %s [%s] - no location permission", sender, app_id)
        return False

    def _get_apparmor_label(self, sender: str) -> str:
        """Get the AppArmor security label for a D-Bus connection."""
        creds = self._bus.call_blocking(
            'org.freedesktop.DBus',
            '/org/freedesktop/DBus',
            'org.freedesktop.DBus',
            'GetConnectionCredentials',
            's', [sender], timeout=5.0)

        label_bytes = creds.get('LinuxSecurityLabel', [])
        if not label_bytes:
            return 'unknown'

        # Decode bytes to string, strip null terminator
        label = bytes(label_bytes).decode('utf-8', errors='replace').rstrip('\x00')
        return label

    def _has_location_policy(self, app_id: str) -> bool:
        """Check if a click app has 'location' in its policy_groups."""
        # Try exact match first
        json_path = os.path.join(CLICK_APPARMOR_DIR, f'{app_id}.json')
        if not os.path.exists(json_path):
            # Try matching with glob pattern (version might differ)
            # Strip the version; keep the '_' so "foo_app" can't match "foo_app2_1.0".
            base = app_id.rsplit('_', 1)[0] if '_' in app_id else app_id
            matches = glob.glob(os.path.join(CLICK_APPARMOR_DIR, f'{base}_*.json'))
            if matches:
                json_path = matches[0]
            else:
                log.debug("No click policy found for %s", app_id)
                return False

        try:
            with open(json_path, 'r') as f:
                policy = json.load(f)
            groups = policy.get('policy_groups', [])
            return 'location' in groups
        except Exception as e:
            log.warning("Error reading policy for %s: %s", app_id, e)
            return False


class LastFixCache:
    """Last GPS fix, kept in memory and persisted to CACHE_FILE (best effort).

    Saved on the first fix, at most every CACHE_SAVE_INTERVAL_SECONDS while GPS
    runs, when GPS stops and on shutdown.
    """

    def __init__(self, path: str = CACHE_FILE):
        self._path = path
        self._fix: Optional[Dict[str, Any]] = None
        self._dirty = False
        self._saved_at: Optional[float] = None
        self._load()

    def _load(self):
        try:
            with open(self._path, 'r') as f:
                fix = json.load(f)
            for key in ('latitude', 'longitude', 'received_at'):
                float(fix[key])
            self._fix = fix
            log.info("Loaded last known fix (%.0f s old)", self.age(fix))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Ignoring unreadable fix cache %s: %s", self._path, e)

    @staticmethod
    def age(fix: Dict[str, Any]) -> float:
        age = time.time() - fix['received_at']
        # A fix from the future means the clock jumped; treat its age as unknown.
        return age if age > -60 else float('inf')

    def get(self) -> Optional[Dict[str, Any]]:
        return self._fix

    def update(self, position: Dict[str, Any]):
        self._fix = dict(position, received_at=time.time())
        self._dirty = True
        if (self._saved_at is None or
                time.monotonic() - self._saved_at >= CACHE_SAVE_INTERVAL_SECONDS):
            self.save()

    def save(self):
        if not self._dirty or self._fix is None:
            return
        try:
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            tmp = self._path + '.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(self._fix, f)
            os.replace(tmp, self._path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError as e:
            log.warning("Could not save fix cache %s: %s", self._path, e)


def stale_position(fix: Dict[str, Any], age: float) -> Dict[str, Any]:
    """Copy of a cached fix with accuracy grown by its age (see CACHED_FIX_DRIFT_M_PER_S)."""
    pos = dict(fix)
    acc = fix.get('horizontal_accuracy') or UNKNOWN_ACCURACY_M
    pos['horizontal_accuracy'] = acc + age * CACHED_FIX_DRIFT_M_PER_S
    pos['heading'] = None
    pos['description'] = f'Last known position ({age:.0f} s old)'
    return pos


class LomiriLocationClient:
    """Asynchronous client for lomiri-location-service.

    Never blocks the main loop: lomiri can hang (e.g. a stuck GNSS HAL), and it
    calls UpdatePosition back on us, so a blocking call stalls every GeoClue2
    client and can deadlock against lomiri's own callbacks.
    """

    def __init__(self, bus: dbus.SystemBus, on_position_update, on_heading_update,
                 on_failure=None):
        self.bus = bus
        self.on_position_update = on_position_update
        self.on_heading_update = on_heading_update
        self.on_failure = on_failure
        self.session_path: Optional[str] = None
        self._session_obj: Optional['LomiriSessionHandler'] = None
        self._starting = False
        # Bumped on every start/stop so late replies for an old session are ignored.
        self._generation = 0

    @property
    def is_active(self) -> bool:
        """True while a session is being created or is running."""
        return self._starting or self.session_path is not None

    def start(self):
        """Request a session; updates start once lomiri replies."""
        if self.is_active:
            log.debug("Lomiri client already active, ignoring start()")
            return
        self._generation += 1
        gen = self._generation
        self._starting = True

        msg = dbus.lowlevel.MethodCallMessage(
            LOMIRI_SERVICE_NAME,
            LOMIRI_SERVICE_PATH,
            LOMIRI_SERVICE_IFACE,
            'CreateSessionForCriteria')
        # Criteria encoding (flat):
        msg.append(True, signature='b')    # requires.position
        msg.append(True, signature='b')    # requires.altitude
        msg.append(True, signature='b')    # requires.heading
        msg.append(False, signature='b')   # requires.velocity
        msg.append(100.0, signature='d')   # accuracy.horizontal (meters)
        msg.append(False, signature='b')   # has vertical_accuracy
        msg.append(False, signature='b')   # has velocity_accuracy
        msg.append(False, signature='b')   # has heading_accuracy

        try:
            self.bus.send_message_with_reply(
                msg, lambda reply: self._on_session_created(gen, reply),
                LOMIRI_CALL_TIMEOUT_SECONDS, require_main_loop=True)
        except Exception as e:
            self._fail(f'CreateSessionForCriteria send failed: {e}')

    def stop(self):
        """Stop updates and forget the session (safe to call multiple times)."""
        if not self.is_active:
            return
        self._teardown(send_stop=True)
        log.info("Lomiri location session stopped")

    def _on_session_created(self, gen, reply):
        if gen != self._generation:
            return  # stopped/restarted while waiting
        self._starting = False
        if reply.get_type() == dbus.lowlevel.MESSAGE_TYPE_ERROR:
            self._fail(f'CreateSessionForCriteria: {reply.get_error_name()} '
                       f'{reply.get_args_list()}')
            return
        try:
            path = str(reply.get_args_list()[0])
        except Exception as e:
            self._fail(f'Bad CreateSessionForCriteria reply: {e}')
            return

        self.session_path = path
        self._session_obj = LomiriSessionHandler(
            self.bus, path, reply.get_sender(),
            self.on_position_update, self.on_heading_update)
        log.info("Created lomiri session: %s", path)
        for method in ('StartPositionUpdates', 'StartHeadingUpdates'):
            self._call_session(method, gen)

    def _call_session(self, method, gen):
        msg = dbus.lowlevel.MethodCallMessage(
            LOMIRI_SERVICE_NAME, self.session_path, LOMIRI_SESSION_IFACE, method)

        def on_reply(reply):
            if gen != self._generation:
                return
            if reply.get_type() == dbus.lowlevel.MESSAGE_TYPE_ERROR:
                log.warning("%s failed: %s %s", method, reply.get_error_name(),
                            reply.get_args_list())
            else:
                log.info("Called %s", method)

        self.bus.send_message_with_reply(
            msg, on_reply, LOMIRI_CALL_TIMEOUT_SECONDS, require_main_loop=True)

    def _fail(self, reason):
        log.error("Lomiri session failed: %s", reason)
        self._teardown(send_stop=False)
        if self.on_failure:
            self.on_failure()

    def _teardown(self, send_stop):
        self._generation += 1
        self._starting = False
        if send_stop and self.session_path:
            for method in ('StopPositionUpdates', 'StopHeadingUpdates'):
                msg = dbus.lowlevel.MethodCallMessage(
                    LOMIRI_SERVICE_NAME, self.session_path, LOMIRI_SESSION_IFACE, method)
                msg.set_no_reply(True)
                try:
                    self.bus.send_message(msg)
                except Exception as e:
                    log.warning("Error sending %s: %s", method, e)
        if self._session_obj:
            self._session_obj.remove_from_connection()
            self._session_obj = None
        self.session_path = None


class LomiriSessionHandler:
    """Handles UpdatePosition/UpdateHeading callbacks from lomiri-location-service
    using a raw message filter (needed because lomiri uses custom flat encoding
    that doesn't match standard D-Bus introspection)."""

    def __init__(self, bus, path, lomiri_owner, on_position, on_heading):
        self._bus = bus
        self._path = path
        # Unique name that created the session. lomiri sends the callbacks from a
        # second connection of the same process, so callers are accepted if
        # their PID matches; anyone else calling UpdatePosition is spoofing.
        self._owner = lomiri_owner
        self._trusted = {lomiri_owner}
        self._untrusted: Set[str] = set()
        self._on_position = on_position
        self._on_heading = on_heading
        # Keep a single bound-method object: libdbus matches filters by the
        # user-data pointer, and removing an unknown filter aborts the process.
        self._filter = self._message_filter
        self._bus.add_message_filter(self._filter)
        log.info("Registered callback handler at path %s (lomiri=%s)", path, lomiri_owner)

    def remove_from_connection(self):
        """Remove the message filter (safe to call multiple times)."""
        if self._filter is None:
            return
        try:
            self._bus.remove_message_filter(self._filter)
        except Exception:
            pass
        self._filter = None

    def _message_filter(self, bus, message):
        """Raw message filter - intercepts UpdatePosition/UpdateHeading calls."""
        if message.get_type() != dbus.lowlevel.MESSAGE_TYPE_METHOD_CALL:
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        if message.get_path() != self._path:
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        member = message.get_member()
        if member not in ('UpdatePosition', 'UpdateHeading'):
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        if not self._is_lomiri(message.get_sender()):
            log.warning("Ignoring %s on %s from %s (not lomiri %s)",
                        member, self._path, message.get_sender(), self._owner)
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        if member == 'UpdatePosition':
            self._handle_position_update(message)
        else:
            self._handle_heading_update(message)
        return dbus.lowlevel.HANDLER_RESULT_HANDLED

    def _is_lomiri(self, sender) -> bool:
        """True if sender is a connection of the lomiri process (result cached)."""
        if sender in self._trusted:
            return True
        if sender in self._untrusted:
            return False
        try:
            pids = [self._bus.call_blocking(
                'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                'GetConnectionUnixProcessID', 's', [name], timeout=2.0)
                for name in (self._owner, sender)]
            ok = pids[0] == pids[1]
        except dbus.DBusException as e:
            log.warning("Could not verify callback sender %s: %s", sender, e)
            ok = False
        if ok:
            log.info("Accepting lomiri callbacks from %s (same process as %s)",
                     sender, self._owner)
            self._trusted.add(sender)
        else:
            self._untrusted.add(sender)
        return ok

    def _handle_position_update(self, message):
        """Parse UpdatePosition: latitude(d), longitude(d),
        has_altitude(b), [altitude(d)],
        has_horiz_acc(b), [horiz_acc(d)],
        has_vert_acc(b), [vert_acc(d)],
        timestamp(x)"""
        try:
            args = message.get_args_list()
            idx = 0
            lat = float(args[idx]); idx += 1
            lon = float(args[idx]); idx += 1

            has_alt = bool(args[idx]); idx += 1
            alt = float(args[idx]) if has_alt else None
            if has_alt:
                idx += 1

            has_hacc = bool(args[idx]); idx += 1
            hacc = float(args[idx]) if has_hacc else None
            if has_hacc:
                idx += 1

            has_vacc = bool(args[idx]); idx += 1
            vacc = float(args[idx]) if has_vacc else None
            if has_vacc:
                idx += 1

            timestamp_ns = int(args[idx]); idx += 1

            position = {
                'latitude': lat,
                'longitude': lon,
                'altitude': alt,
                'horizontal_accuracy': hacc,
                'vertical_accuracy': vacc,
                'timestamp': timestamp_ns
            }

            log.debug("Position update: lat=%.6f lon=%.6f alt=%s acc=%s",
                      lat, lon, alt, hacc)
            self._on_position(position)
        except Exception as e:
            log.error("Error parsing position update: %s (args=%s)", e, message.get_args_list())

        # Send reply
        reply = dbus.lowlevel.MethodReturnMessage(message)
        self._bus.send_message(reply)

    def _handle_heading_update(self, message):
        """Parse UpdateHeading: heading(d), timestamp(x)"""
        try:
            args = message.get_args_list()
            heading = float(args[0])
            timestamp_ns = int(args[1])
            self._on_heading({'heading': heading, 'timestamp': timestamp_ns})
        except Exception as e:
            log.error("Error parsing heading update: %s", e)

        reply = dbus.lowlevel.MethodReturnMessage(message)
        self._bus.send_message(reply)


def _check_owner(owner: Optional[str], sender: Optional[str]):
    """Only the peer that obtained a client may control it or read its locations."""
    if owner and sender and sender != owner:
        raise dbus.exceptions.DBusException(
            'Object belongs to another D-Bus peer', name='org.freedesktop.DBus.Error.AccessDenied')


def _value_or(value, default):
    return default if value is None else value


class GeoClue2Location(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Location interface."""

    IFACE = 'org.freedesktop.GeoClue2.Location'

    def __init__(self, bus, path, position: Dict[str, Any], owner: Optional[str]):
        super().__init__(bus, path)
        self._owner = owner
        self._latitude = position.get('latitude', 0.0)
        self._longitude = position.get('longitude', 0.0)
        self._accuracy = _value_or(position.get('horizontal_accuracy'), UNKNOWN_ACCURACY_M)
        self._altitude = _value_or(position.get('altitude'), GCLUE_ALTITUDE_UNKNOWN)
        self._speed = _value_or(position.get('speed'), GCLUE_UNKNOWN)
        self._heading = _value_or(position.get('heading'), GCLUE_UNKNOWN)
        ts_ns = position.get('timestamp') or 0
        self._timestamp = (int(ts_ns // 1_000_000_000), int((ts_ns % 1_000_000_000) // 1000))
        self._description = position.get('description', '')

    def _props(self):
        return {
            'Latitude': dbus.Double(self._latitude),
            'Longitude': dbus.Double(self._longitude),
            'Accuracy': dbus.Double(self._accuracy),
            'Altitude': dbus.Double(self._altitude),
            'Speed': dbus.Double(self._speed),
            'Heading': dbus.Double(self._heading),
            'Description': dbus.String(self._description),
            'Timestamp': dbus.Struct((dbus.UInt64(self._timestamp[0]),
                                      dbus.UInt64(self._timestamp[1])),
                                     signature='tt'),
        }

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v',
                         sender_keyword='sender')
    def Get(self, interface, prop, sender=None):
        _check_owner(self._owner, sender)
        if interface != self.IFACE:
            raise dbus.exceptions.DBusException(
                f'No such interface: {interface}', name='org.freedesktop.DBus.Error.InvalidArgs')
        props = self._props()
        if prop not in props:
            raise dbus.exceptions.DBusException(
                f'No such property: {prop}', name='org.freedesktop.DBus.Error.InvalidArgs')
        return props[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}',
                         sender_keyword='sender')
    def GetAll(self, interface, sender=None):
        _check_owner(self._owner, sender)
        if interface != self.IFACE:
            return {}
        return self._props()


class GeoClue2Client(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Client interface."""

    IFACE = 'org.freedesktop.GeoClue2.Client'

    def __init__(self, bus, path, client_id, owner, on_active_change, on_started=None):
        super().__init__(bus, path)
        self._bus = bus
        self._path = path
        self._client_id = client_id
        self.owner = owner
        self._on_active_change = on_active_change
        self._on_started = on_started
        self._desktop_id = ''
        self._requested_accuracy_level = GCLUE_ACCURACY_LEVEL_EXACT
        self._distance_threshold = 0
        self._time_threshold = 0
        self._active = False
        self._location_path = dbus.ObjectPath('/')
        self._location_obj: Optional[GeoClue2Location] = None
        self._location_counter = 0
        self._destroyed = False
        # Bumped on every Start() so pending cached-fix timers from an earlier
        # Start/Stop cycle don't fire into this one.
        self.start_generation = 0
        self.got_location_since_start = False

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def active(self) -> bool:
        return self._active

    def destroy(self):
        """Clean up this client and its location objects."""
        if self._destroyed:
            return
        self._destroyed = True
        was_active = self._active
        self._active = False
        if self._location_obj:
            try:
                self._location_obj.remove_from_connection()
            except Exception:
                pass
            self._location_obj = None
        try:
            self.remove_from_connection()
        except Exception:
            pass
        if was_active:
            self._on_active_change()
        log.info("Client %s destroyed", self._client_id)

    def update_location(self, position: Dict[str, Any]):
        """Publish a new Location object and emit LocationUpdated."""
        if self._destroyed or not self._active:
            return
        self.got_location_since_start = True
        old_path = self._location_path
        self._location_counter += 1
        new_path = dbus.ObjectPath(f'{self._path}/Location/{self._location_counter}')

        if self._location_obj:
            self._location_obj.remove_from_connection()

        self._location_obj = GeoClue2Location(self._bus, new_path, position, self.owner)
        self._location_path = new_path

        self.LocationUpdated(old_path, new_path)
        # Debug only: coordinates must not end up in the persistent journal.
        log.debug("LocationUpdated: %s -> %s (lat=%.6f lon=%.6f)",
                  old_path, new_path,
                  position.get('latitude', 0), position.get('longitude', 0))

    @dbus.service.method(IFACE, in_signature='', out_signature='',
                         sender_keyword='sender')
    def Start(self, sender=None):
        _check_owner(self.owner, sender)
        if self._destroyed:
            return
        log.info("Client %s started (desktop_id=%s)", self._client_id, self._desktop_id)
        self._active = True
        self.start_generation += 1
        self.got_location_since_start = False
        self._on_active_change()
        if self._on_started:
            self._on_started(self)

    @dbus.service.method(IFACE, in_signature='', out_signature='',
                         sender_keyword='sender')
    def Stop(self, sender=None):
        _check_owner(self.owner, sender)
        if self._destroyed:
            return
        log.info("Client %s stopped", self._client_id)
        self._active = False
        self._on_active_change()

    @dbus.service.signal(IFACE, signature='oo')
    def LocationUpdated(self, old_location, new_location):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self._get_prop(interface, prop)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != self.IFACE:
            return {}
        return {
            'DesktopId': dbus.String(self._desktop_id),
            'RequestedAccuracyLevel': dbus.UInt32(self._requested_accuracy_level),
            'DistanceThreshold': dbus.UInt32(self._distance_threshold),
            'TimeThreshold': dbus.UInt32(self._time_threshold),
            'Location': dbus.ObjectPath(self._location_path),
            'Active': dbus.Boolean(self._active),
        }

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv', out_signature='',
                         sender_keyword='sender')
    def Set(self, interface, prop, value, sender=None):
        _check_owner(self.owner, sender)
        if interface != self.IFACE:
            return
        if prop == 'DesktopId':
            self._desktop_id = str(value)
        elif prop == 'RequestedAccuracyLevel':
            self._requested_accuracy_level = int(value)
        elif prop == 'DistanceThreshold':
            self._distance_threshold = int(value)
        elif prop == 'TimeThreshold':
            self._time_threshold = int(value)

    def _get_prop(self, interface, prop):
        if interface != self.IFACE:
            raise dbus.exceptions.DBusException(
                f'No such interface: {interface}', name='org.freedesktop.DBus.Error.InvalidArgs')
        all_props = self.GetAll(interface)
        if prop not in all_props:
            raise dbus.exceptions.DBusException(
                f'No such property: {prop}', name='org.freedesktop.DBus.Error.InvalidArgs')
        return all_props[prop]


class GeoClue2Manager(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Manager interface.

    Manages client lifecycle, monitors for disconnects, and controls
    GPS activation (only active when at least one client is listening).
    """

    IFACE = 'org.freedesktop.GeoClue2.Manager'

    def __init__(self, bus, confinement_checker: Optional['ConfinementChecker'] = None,
                 on_gps_needed=None, on_gps_not_needed=None,
                 last_fix: Optional[LastFixCache] = None):
        super().__init__(bus, '/org/freedesktop/GeoClue2/Manager')
        self._bus = bus
        self._clients: Dict[str, GeoClue2Client] = {}
        self._client_counter = 0
        self._checker = confinement_checker
        self._on_gps_needed = on_gps_needed
        self._on_gps_not_needed = on_gps_not_needed
        self._last_fix = last_fix
        self._gps_stop_timer_id: Optional[int] = None

    def _active_count(self) -> int:
        return sum(1 for c in self._clients.values() if c.active)

    def _on_client_active_change(self):
        """Called when any client's active state changes. Manages GPS lifecycle."""
        active_count = self._active_count()
        log.debug("Active client count: %d", active_count)

        if active_count > 0:
            # Cancel any pending GPS stop
            if self._gps_stop_timer_id is not None:
                GLib.source_remove(self._gps_stop_timer_id)
                self._gps_stop_timer_id = None
            if self._on_gps_needed:
                self._on_gps_needed()
        elif active_count == 0:
            # Delay GPS stop to allow for quick re-requests
            if self._gps_stop_timer_id is None and self._on_gps_not_needed:
                self._gps_stop_timer_id = GLib.timeout_add_seconds(
                    GPS_STOP_DELAY_SECONDS, self._delayed_gps_stop)

    def _delayed_gps_stop(self):
        """Called after delay when no active clients remain."""
        self._gps_stop_timer_id = None
        if self._active_count() == 0 and self._on_gps_not_needed:
            log.info("No active clients, stopping GPS")
            self._on_gps_not_needed()
        return False  # Don't repeat

    def _on_client_started(self, client: GeoClue2Client):
        """Give a newly started client a position without waiting for the next GPS fix.

        A fix that is still fresh (GPS already running) is sent right away. Otherwise
        GPS gets CACHED_FIX_GRACE_SECONDS to deliver a real fix before the last known
        one is sent with its accuracy degraded by age.
        """
        if not self._last_fix:
            return
        fix = self._last_fix.get()
        gen = client.start_generation
        if fix and LastFixCache.age(fix) <= FRESH_FIX_SECONDS:
            GLib.idle_add(self._send_last_fix, client, gen)
        else:
            GLib.timeout_add_seconds(CACHED_FIX_GRACE_SECONDS,
                                     self._send_last_fix, client, gen)

    def _send_last_fix(self, client: GeoClue2Client, gen: int):
        if (client.destroyed or not client.active or client.start_generation != gen
                or client.got_location_since_start):
            return False
        fix = self._last_fix.get()
        if not fix:
            return False
        age = LastFixCache.age(fix)
        if age > CACHED_FIX_MAX_AGE_SECONDS:
            log.info("Last known fix too old (%.0f s), waiting for GPS", age)
            return False
        if age <= FRESH_FIX_SECONDS:
            client.update_location(fix)
        else:
            log.info("No GPS fix yet; sending last known fix (%.0f s old) to client %s",
                     age, client._client_id)
            client.update_location(stale_position(fix, age))
        return False

    def _remove_client(self, path: str):
        """Remove a client and clean up its resources."""
        client = self._clients.pop(path, None)
        if client:
            client.destroy()

    def on_owner_disconnected(self, sender: str):
        """Called when a D-Bus name disappears. Cleans up its clients."""
        paths_to_remove = [
            path for path, client in self._clients.items()
            if client.owner == sender
        ]
        for path in paths_to_remove:
            log.info("Client at %s disconnected (owner %s gone)", path, sender)
            self._remove_client(path)

    def _create_client(self, sender):
        if self._checker and sender:
            if not self._checker.check_access(sender):
                raise dbus.exceptions.DBusException(
                    'App does not have the location permission', name='org.freedesktop.GeoClue2.Error.PermissionDenied')

        self._client_counter += 1
        path = f'/org/freedesktop/GeoClue2/Client/{self._client_counter}'
        client = GeoClue2Client(self._bus, path, self._client_counter, sender,
                                on_active_change=self._on_client_active_change,
                                on_started=self._on_client_started)
        self._clients[path] = client
        log.info("Created GeoClue2 client: %s (sender=%s)", path, sender)
        return dbus.ObjectPath(path)

    @dbus.service.method(IFACE, in_signature='', out_signature='o',
                         sender_keyword='sender')
    def GetClient(self, sender=None):
        """Like real GeoClue: one client per peer, reused on repeated calls."""
        if sender:
            for path, client in self._clients.items():
                if client.owner == sender and not client.destroyed:
                    return dbus.ObjectPath(path)
        return self._create_client(sender)

    @dbus.service.method(IFACE, in_signature='', out_signature='o',
                         sender_keyword='sender')
    def CreateClient(self, sender=None):
        """Always creates a new client (GeoClue >= 2.5 API)."""
        return self._create_client(sender)

    @dbus.service.method(IFACE, in_signature='o', out_signature='',
                         sender_keyword='sender')
    def DeleteClient(self, client_path, sender=None):
        client = self._clients.get(str(client_path))
        if not client:
            raise dbus.exceptions.DBusException(
                f'No such client: {client_path}', name='org.freedesktop.DBus.Error.InvalidArgs')
        _check_owner(client.owner, sender)
        self._remove_client(str(client_path))

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        if interface != self.IFACE:
            raise dbus.exceptions.DBusException(
                f'No such interface: {interface}', name='org.freedesktop.DBus.Error.InvalidArgs')
        props = self.GetAll(interface)
        if prop not in props:
            raise dbus.exceptions.DBusException(
                f'No such property: {prop}', name='org.freedesktop.DBus.Error.InvalidArgs')
        return props[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != self.IFACE:
            return {}
        return {
            'InUse': dbus.Boolean(self._active_count() > 0),
            'AvailableAccuracyLevel': dbus.UInt32(GCLUE_ACCURACY_LEVEL_EXACT),
        }

    def broadcast_position(self, position: Dict[str, Any]):
        """Send position update to all active clients."""
        for client in list(self._clients.values()):
            if client.active:
                client.update_location(position)


class GeoClue2Bridge:
    """Main bridge orchestrator.

    Manages the lifecycle of:
    - GeoClue2 D-Bus service (always running)
    - Lomiri location client (started/stopped based on demand)
    - Client disconnect monitoring
    - Reconnection to lomiri-location-service
    - Last known fix cache
    """

    def __init__(self):
        self.loop = GLib.MainLoop()
        self.bus: Optional[dbus.SystemBus] = None
        self.manager: Optional[GeoClue2Manager] = None
        self.lomiri_client: Optional[LomiriLocationClient] = None
        self._checker: Optional[ConfinementChecker] = None
        self._last_heading: Optional[float] = None
        self._last_fix = LastFixCache()
        self._gps_wanted = False
        self._reconnect_timer_id: Optional[int] = None

    def run(self):
        """Start the bridge."""
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()

        # Claim the GeoClue2 bus name
        try:
            bus_name = dbus.service.BusName(  # noqa: F841 - must stay referenced
                GEOCLUE2_BUS_NAME,
                self.bus,
                do_not_queue=True)
        except dbus.exceptions.NameExistsException:
            log.error("Another GeoClue2 service is already running!")
            sys.exit(1)

        log.info("Registered %s on system bus", GEOCLUE2_BUS_NAME)

        self._checker = ConfinementChecker(self.bus)

        # SIGHUP reloads whitelist; SIGTERM/SIGINT shut down cleanly (stop the
        # lomiri session, save the fix cache).
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._do_reload)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._quit)

        self.manager = GeoClue2Manager(
            self.bus,
            confinement_checker=self._checker,
            on_gps_needed=self._start_gps,
            on_gps_not_needed=self._stop_gps,
            last_fix=self._last_fix)
        log.info("GeoClue2 Manager ready at /org/freedesktop/GeoClue2/Manager")
        log.info("Confinement: only apps with 'location' policy group or whitelisted apps can access")
        log.info("GPS lifecycle: GPS is OFF until a client calls Start()")

        # Prepare lomiri client (but don't start it yet — lazy activation)
        self.lomiri_client = LomiriLocationClient(
            self.bus,
            on_position_update=self._on_position,
            on_heading_update=self._on_heading,
            on_failure=self._on_lomiri_failure)

        # Monitor for lomiri-location-service appearing/disappearing
        self.bus.add_signal_receiver(
            self._on_lomiri_name_change,
            signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus',
            arg0=LOMIRI_SERVICE_NAME)

        # Monitor for GeoClue2 client owners disconnecting
        self.bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus')

        log.info("GeoClue2 bridge running (GPS inactive).")
        try:
            self.loop.run()
        finally:
            log.info("Shutting down...")
            self._stop_gps()
            self.bus.flush()

    def _quit(self):
        self.loop.quit()
        return GLib.SOURCE_CONTINUE

    def _do_reload(self):
        if self._checker:
            self._checker.reload_whitelist()
        return GLib.SOURCE_CONTINUE

    def _start_gps(self):
        """Start GPS (lomiri session). Called when first client needs position."""
        if self._gps_wanted:
            return
        self._gps_wanted = True

        if self._reconnect_timer_id is not None:
            GLib.source_remove(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        if self.lomiri_client.is_active:
            return

        log.info("Starting GPS (client requested position)")
        self.lomiri_client.start()

    def _stop_gps(self):
        """Stop GPS (lomiri session). Called when no clients need position."""
        self._gps_wanted = False
        if self._reconnect_timer_id is not None:
            GLib.source_remove(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        if self.lomiri_client and self.lomiri_client.is_active:
            log.info("Stopping GPS (no active clients)")
            self.lomiri_client.stop()
        self._last_heading = None
        self._last_fix.save()

    def _on_lomiri_failure(self):
        if self._gps_wanted:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt to lomiri."""
        if self._reconnect_timer_id is not None:
            return
        log.info("Will retry lomiri connection in %ds", RECONNECT_DELAY_SECONDS)
        self._reconnect_timer_id = GLib.timeout_add_seconds(
            RECONNECT_DELAY_SECONDS, self._try_reconnect)

    def _try_reconnect(self):
        """Attempt to reconnect to lomiri-location-service."""
        self._reconnect_timer_id = None
        if not self._gps_wanted or self.lomiri_client.is_active:
            return False
        log.info("Attempting to reconnect to lomiri-location-service...")
        self.lomiri_client.start()
        return False  # Don't repeat

    def _on_lomiri_name_change(self, name, old_owner, new_owner):
        """Called when lomiri-location-service appears or disappears."""
        if old_owner and self.lomiri_client.is_active:
            log.warning("lomiri-location-service disappeared from bus!")
            self.lomiri_client.stop()
            if self._gps_wanted:
                self._schedule_reconnect()
        if new_owner:
            log.info("lomiri-location-service appeared on bus")
            if self._reconnect_timer_id is not None:
                GLib.source_remove(self._reconnect_timer_id)
                self._reconnect_timer_id = None
            if self._gps_wanted and not self.lomiri_client.is_active:
                GLib.idle_add(self._try_reconnect)

    def _on_name_owner_changed(self, name, old_owner, new_owner):
        """Monitor for GeoClue2 client owners disappearing."""
        # A unique name (':1.42') disappears when new_owner is empty
        if not new_owner and old_owner:
            if self.manager:
                self.manager.on_owner_disconnected(old_owner)

    def _on_position(self, position: Dict[str, Any]):
        """Called when lomiri sends a position update (~1Hz)."""
        position['heading'] = self._last_heading
        self._last_fix.update(position)
        if self.manager:
            self.manager.broadcast_position(position)

    def _on_heading(self, heading_data: Dict[str, Any]):
        """Called when lomiri sends a heading update."""
        self._last_heading = heading_data.get('heading')
        # Don't re-broadcast just for heading changes — it causes map jumping.
        # The heading will be merged into the next real position update.


def main():
    if '--debug' in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)

    bridge = GeoClue2Bridge()
    bridge.run()


if __name__ == '__main__':
    main()
