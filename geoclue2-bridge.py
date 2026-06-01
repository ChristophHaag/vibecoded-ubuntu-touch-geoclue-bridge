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
    disconnected clients, reloads whitelist on SIGHUP
"""

import json
import os
import signal
import sys

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
            's', [sender])

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
            import glob
            base = app_id.rsplit('_', 1)[0] if '_' in app_id else app_id
            matches = glob.glob(os.path.join(CLICK_APPARMOR_DIR, f'{base}*.json'))
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


class LomiriLocationClient:
    """Client for lomiri-location-service.

    Handles session creation and receives position updates via D-Bus method
    callbacks from the service. Supports start/stop lifecycle and reconnection.
    """

    def __init__(self, bus: dbus.SystemBus, on_position_update, on_heading_update):
        self.bus = bus
        self.on_position_update = on_position_update
        self.on_heading_update = on_heading_update
        self.session_path: Optional[str] = None
        self._session_obj: Optional['LomiriSessionHandler'] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self):
        """Create a session and start receiving position updates."""
        if self._running:
            log.debug("Lomiri client already running, ignoring start()")
            return
        try:
            self._create_session()
            self._register_callback_handler()
            self._start_updates()
            self._running = True
            log.info("Lomiri location session active at %s", self.session_path)
        except Exception as e:
            log.error("Failed to start lomiri location client: %s", e)
            self._cleanup()
            raise

    def stop(self):
        """Stop position updates and tear down session."""
        if not self._running and not self.session_path:
            return
        self._running = False
        try:
            self._stop_updates()
        except Exception as e:
            log.warning("Error stopping updates: %s", e)
        self._cleanup()
        log.info("Lomiri location session stopped")

    def _cleanup(self):
        """Clean up session state (safe to call multiple times)."""
        if self._session_obj:
            self._session_obj.remove_from_connection()
            self._session_obj = None
        self.session_path = None

    def _create_session(self):
        """Call CreateSessionForCriteria with flat-encoded Criteria struct."""
        msg = dbus.lowlevel.MethodCallMessage(
            LOMIRI_SERVICE_NAME,
            LOMIRI_SERVICE_PATH,
            LOMIRI_SERVICE_IFACE,
            'CreateSessionForCriteria')

        # Criteria encoding (flat):
        # bool requires.position = true
        # bool requires.altitude = true
        # bool requires.heading = true
        # bool requires.velocity = false
        # double accuracy.horizontal = 100.0 (meters)
        # bool has_vertical_accuracy = false
        # bool has_velocity_accuracy = false
        # bool has_heading_accuracy = false
        msg.append(True, signature='b')    # requires.position
        msg.append(True, signature='b')    # requires.altitude
        msg.append(True, signature='b')    # requires.heading
        msg.append(False, signature='b')   # requires.velocity
        msg.append(100.0, signature='d')   # accuracy.horizontal (meters)
        msg.append(False, signature='b')   # has vertical_accuracy
        msg.append(False, signature='b')   # has velocity_accuracy
        msg.append(False, signature='b')   # has heading_accuracy

        reply = self.bus.send_message_with_reply_and_block(msg, 10000)
        self.session_path = reply.get_args_list()[0]
        log.info("Created lomiri session: %s", self.session_path)

    def _register_callback_handler(self):
        """Register a D-Bus object to receive UpdatePosition callbacks."""
        self._session_obj = LomiriSessionHandler(
            self.bus, self.session_path,
            self.on_position_update,
            self.on_heading_update)

    def _start_updates(self):
        """Call StartPositionUpdates and StartHeadingUpdates on the session."""
        for method in ('StartPositionUpdates', 'StartHeadingUpdates'):
            msg = dbus.lowlevel.MethodCallMessage(
                LOMIRI_SERVICE_NAME,
                self.session_path,
                LOMIRI_SESSION_IFACE,
                method)
            try:
                self.bus.send_message_with_reply_and_block(msg, 5000)
                log.info("Called %s", method)
            except dbus.DBusException as e:
                log.warning("Failed to call %s: %s", method, e)

    def _stop_updates(self):
        """Call StopPositionUpdates on the session."""
        for method in ('StopPositionUpdates', 'StopHeadingUpdates'):
            msg = dbus.lowlevel.MethodCallMessage(
                LOMIRI_SERVICE_NAME,
                self.session_path,
                LOMIRI_SESSION_IFACE,
                method)
            try:
                self.bus.send_message_with_reply_and_block(msg, 5000)
            except dbus.DBusException:
                pass


class LomiriSessionHandler:
    """Handles UpdatePosition/UpdateHeading callbacks from lomiri-location-service
    using a raw message filter (needed because lomiri uses custom flat encoding
    that doesn't match standard D-Bus introspection)."""

    def __init__(self, bus, path, on_position, on_heading):
        self._bus = bus
        self._path = path
        self._on_position = on_position
        self._on_heading = on_heading
        # Register a message filter to intercept method calls at our session path
        self._bus.add_message_filter(self._message_filter)
        log.info("Registered callback handler at path %s", path)

    def remove_from_connection(self):
        """Remove the message filter."""
        try:
            self._bus.remove_message_filter(self._message_filter)
        except Exception:
            pass

    def _message_filter(self, bus, message):
        """Raw message filter - intercepts UpdatePosition/UpdateHeading calls."""
        if message.get_type() != 1:  # METHOD_CALL = 1
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        if message.get_path() != self._path:
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

        member = message.get_member()

        if member == 'UpdatePosition':
            self._handle_position_update(message)
            return dbus.lowlevel.HANDLER_RESULT_HANDLED
        elif member == 'UpdateHeading':
            self._handle_heading_update(message)
            return dbus.lowlevel.HANDLER_RESULT_HANDLED

        return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED

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


class GeoClue2Location(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Location interface."""

    IFACE = 'org.freedesktop.GeoClue2.Location'

    def __init__(self, bus, path, position: Dict[str, Any]):
        super().__init__(bus, path)
        self._latitude = position.get('latitude', 0.0)
        self._longitude = position.get('longitude', 0.0)
        self._accuracy = position.get('horizontal_accuracy') or 0.0
        self._altitude = position.get('altitude') or 0.0
        self._speed = position.get('speed') or -1.0
        self._heading = position.get('heading') or -1.0
        ts_ns = position.get('timestamp', 0)
        self._timestamp = (int(ts_ns // 1_000_000_000), int((ts_ns % 1_000_000_000) // 1000))
        self._description = ''

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self._get_prop(interface, prop)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != self.IFACE:
            return {}
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

    def _get_prop(self, interface, prop):
        if interface != self.IFACE:
            raise dbus.exceptions.DBusException(
                'org.freedesktop.DBus.Error.InvalidArgs',
                f'No such interface: {interface}')
        props = {
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
        if prop not in props:
            raise dbus.exceptions.DBusException(
                'org.freedesktop.DBus.Error.InvalidArgs',
                f'No such property: {prop}')
        return props[prop]


class GeoClue2Client(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Client interface."""

    IFACE = 'org.freedesktop.GeoClue2.Client'

    def __init__(self, bus, path, client_id, on_active_change):
        super().__init__(bus, path)
        self._bus = bus
        self._path = path
        self._client_id = client_id
        self._on_active_change = on_active_change
        self._desktop_id = ''
        self._requested_accuracy_level = GCLUE_ACCURACY_LEVEL_EXACT
        self._distance_threshold = 0
        self._time_threshold = 0
        self._active = False
        self._location_path = dbus.ObjectPath('/')
        self._location_obj: Optional[GeoClue2Location] = None
        self._location_counter = 0
        self._destroyed = False

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
        """Called when a new position arrives from lomiri."""
        if self._destroyed or not self._active:
            return
        old_path = self._location_path
        self._location_counter += 1
        new_path = dbus.ObjectPath(f'{self._path}/Location/{self._location_counter}')

        # Remove old location object
        if self._location_obj:
            self._location_obj.remove_from_connection()

        # Create new location object
        self._location_obj = GeoClue2Location(self._bus, new_path, position)
        self._location_path = new_path

        # Emit LocationUpdated signal
        self.LocationUpdated(old_path, new_path)
        log.info("LocationUpdated: %s -> %s (lat=%.6f lon=%.6f)",
                 old_path, new_path,
                 position.get('latitude', 0), position.get('longitude', 0))

    @dbus.service.method(IFACE, in_signature='', out_signature='')
    def Start(self):
        if self._destroyed:
            return
        log.info("Client %s started (desktop_id=%s)", self._client_id, self._desktop_id)
        self._active = True
        self._on_active_change()

    @dbus.service.method(IFACE, in_signature='', out_signature='')
    def Stop(self):
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

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv', out_signature='')
    def Set(self, interface, prop, value):
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
                'org.freedesktop.DBus.Error.InvalidArgs',
                f'No such interface: {interface}')
        all_props = self.GetAll(interface)
        if prop not in all_props:
            raise dbus.exceptions.DBusException(
                'org.freedesktop.DBus.Error.InvalidArgs',
                f'No such property: {prop}')
        return all_props[prop]


class GeoClue2Manager(dbus.service.Object):
    """Implements org.freedesktop.GeoClue2.Manager interface.

    Manages client lifecycle, monitors for disconnects, and controls
    GPS activation (only active when at least one client is listening).
    """

    IFACE = 'org.freedesktop.GeoClue2.Manager'

    def __init__(self, bus, confinement_checker: Optional['ConfinementChecker'] = None,
                 on_gps_needed=None, on_gps_not_needed=None):
        super().__init__(bus, '/org/freedesktop/GeoClue2/Manager')
        self._bus = bus
        self._clients: Dict[str, GeoClue2Client] = {}
        self._client_owners: Dict[str, str] = {}  # path -> sender unique name
        self._client_counter = 0
        self._checker = confinement_checker
        self._on_gps_needed = on_gps_needed
        self._on_gps_not_needed = on_gps_not_needed
        self._gps_stop_timer_id: Optional[int] = None

    def _on_client_active_change(self):
        """Called when any client's active state changes. Manages GPS lifecycle."""
        active_count = sum(1 for c in self._clients.values() if c._active)
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
        active_count = sum(1 for c in self._clients.values() if c._active)
        if active_count == 0 and self._on_gps_not_needed:
            log.info("No active clients, stopping GPS")
            self._on_gps_not_needed()
        return False  # Don't repeat

    def _remove_client(self, path: str):
        """Remove a client and clean up its resources."""
        client = self._clients.pop(path, None)
        self._client_owners.pop(path, None)
        if client:
            client.destroy()

    def on_owner_disconnected(self, sender: str):
        """Called when a D-Bus name disappears. Cleans up its clients."""
        paths_to_remove = [
            path for path, owner in self._client_owners.items()
            if owner == sender
        ]
        for path in paths_to_remove:
            log.info("Client at %s disconnected (owner %s gone)", path, sender)
            self._remove_client(path)

    @dbus.service.method(IFACE, in_signature='', out_signature='o',
                         sender_keyword='sender')
    def GetClient(self, sender=None):
        # Check confinement: only apps with 'location' permission may access
        if self._checker and sender:
            if not self._checker.check_access(sender):
                raise dbus.exceptions.DBusException(
                    'org.freedesktop.GeoClue2.Error.PermissionDenied',
                    'App does not have the location permission')

        self._client_counter += 1
        path = f'/org/freedesktop/GeoClue2/Client/{self._client_counter}'
        client = GeoClue2Client(self._bus, path, self._client_counter,
                                on_active_change=self._on_client_active_change)
        self._clients[path] = client
        if sender:
            self._client_owners[path] = sender
        log.info("Created GeoClue2 client: %s (sender=%s)", path, sender)
        return dbus.ObjectPath(path)

    @dbus.service.method(IFACE, in_signature='', out_signature='o',
                         sender_keyword='sender')
    def CreateClient(self, sender=None):
        """Alias for GetClient (some implementations use this name)."""
        return self.GetClient(sender=sender)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        if interface != self.IFACE:
            raise dbus.exceptions.DBusException(
                'org.freedesktop.DBus.Error.InvalidArgs',
                f'No such interface: {interface}')
        if prop == 'InUse':
            return dbus.Boolean(any(c._active for c in self._clients.values()))
        elif prop == 'AvailableAccuracyLevel':
            return dbus.UInt32(GCLUE_ACCURACY_LEVEL_EXACT)
        raise dbus.exceptions.DBusException(
            'org.freedesktop.DBus.Error.InvalidArgs',
            f'No such property: {prop}')

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != self.IFACE:
            return {}
        return {
            'InUse': dbus.Boolean(any(c._active for c in self._clients.values())),
            'AvailableAccuracyLevel': dbus.UInt32(GCLUE_ACCURACY_LEVEL_EXACT),
        }

    def broadcast_position(self, position: Dict[str, Any]):
        """Send position update to all active clients."""
        for client in self._clients.values():
            if client._active:
                client.update_location(position)


class GeoClue2Bridge:
    """Main bridge orchestrator.

    Manages the lifecycle of:
    - GeoClue2 D-Bus service (always running)
    - Lomiri location client (started/stopped based on demand)
    - Client disconnect monitoring
    - Reconnection to lomiri-location-service
    """

    def __init__(self):
        self.loop = GLib.MainLoop()
        self.bus: Optional[dbus.SystemBus] = None
        self.manager: Optional[GeoClue2Manager] = None
        self.lomiri_client: Optional[LomiriLocationClient] = None
        self._checker: Optional[ConfinementChecker] = None
        self._last_heading: Optional[float] = None
        self._last_position: Optional[Dict[str, Any]] = None
        self._gps_wanted = False
        self._reconnect_timer_id: Optional[int] = None

    def run(self):
        """Start the bridge."""
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()

        # Claim the GeoClue2 bus name
        try:
            bus_name = dbus.service.BusName(
                GEOCLUE2_BUS_NAME,
                self.bus,
                do_not_queue=True)
        except dbus.exceptions.NameExistsException:
            log.error("Another GeoClue2 service is already running!")
            sys.exit(1)

        log.info("Registered %s on system bus", GEOCLUE2_BUS_NAME)

        # Set up confinement checker
        self._checker = ConfinementChecker(self.bus)

        # SIGHUP reloads whitelist
        signal.signal(signal.SIGHUP, self._handle_sighup)

        # Create GeoClue2 Manager with GPS lifecycle callbacks
        self.manager = GeoClue2Manager(
            self.bus,
            confinement_checker=self._checker,
            on_gps_needed=self._start_gps,
            on_gps_not_needed=self._stop_gps)
        log.info("GeoClue2 Manager ready at /org/freedesktop/GeoClue2/Manager")
        log.info("Confinement: only apps with 'location' policy group or whitelisted apps can access")
        log.info("GPS lifecycle: GPS is OFF until a client calls Start()")

        # Prepare lomiri client (but don't start it yet — lazy activation)
        self.lomiri_client = LomiriLocationClient(
            self.bus,
            on_position_update=self._on_position,
            on_heading_update=self._on_heading)

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

        log.info("GeoClue2 bridge running (GPS inactive). Press Ctrl+C to stop.")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            self._stop_gps()

    def _handle_sighup(self, signum, frame):
        """Reload whitelist on SIGHUP."""
        GLib.idle_add(self._do_reload)

    def _do_reload(self):
        """Reload on main loop (signal-safe)."""
        if self._checker:
            self._checker.reload_whitelist()
        return False

    def _start_gps(self):
        """Start GPS (lomiri session). Called when first client needs position."""
        if self._gps_wanted:
            return
        self._gps_wanted = True

        # Cancel any pending reconnect timer
        if self._reconnect_timer_id is not None:
            GLib.source_remove(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        if self.lomiri_client and self.lomiri_client.is_running:
            return

        log.info("Starting GPS (client requested position)")
        try:
            self.lomiri_client.start()
        except Exception as e:
            log.error("Cannot connect to lomiri-location-service: %s", e)
            self._schedule_reconnect()

    def _stop_gps(self):
        """Stop GPS (lomiri session). Called when no clients need position."""
        self._gps_wanted = False
        if self._reconnect_timer_id is not None:
            GLib.source_remove(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        if self.lomiri_client and self.lomiri_client.is_running:
            log.info("Stopping GPS (no active clients)")
            self.lomiri_client.stop()
            self._last_position = None
            self._last_heading = None

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
        if not self._gps_wanted:
            return False
        # Already connected — nothing to do (guards against double-fire)
        if self.lomiri_client and self.lomiri_client.is_running:
            return False

        log.info("Attempting to reconnect to lomiri-location-service...")
        try:
            if self.lomiri_client:
                self.lomiri_client.stop()
            self.lomiri_client = LomiriLocationClient(
                self.bus,
                on_position_update=self._on_position,
                on_heading_update=self._on_heading)
            self.lomiri_client.start()
            log.info("Reconnected to lomiri-location-service successfully")
        except Exception as e:
            log.warning("Reconnection failed: %s", e)
            self._schedule_reconnect()
        return False  # Don't repeat

    def _on_lomiri_name_change(self, name, old_owner, new_owner):
        """Called when lomiri-location-service appears or disappears."""
        if new_owner and not old_owner:
            log.info("lomiri-location-service appeared on bus")
            # Cancel any pending reconnect timer — we'll reconnect via idle
            if self._reconnect_timer_id is not None:
                GLib.source_remove(self._reconnect_timer_id)
                self._reconnect_timer_id = None
            if self._gps_wanted and self.lomiri_client and not self.lomiri_client.is_running:
                GLib.idle_add(self._try_reconnect)
        elif old_owner and not new_owner:
            log.warning("lomiri-location-service disappeared from bus!")
            if self.lomiri_client and self.lomiri_client.is_running:
                self.lomiri_client.stop()
            if self._gps_wanted:
                self._schedule_reconnect()

    def _on_name_owner_changed(self, name, old_owner, new_owner):
        """Monitor for GeoClue2 client owners disappearing."""
        # A unique name (':1.42') disappears when new_owner is empty
        if not new_owner and old_owner:
            # For unique names: name == old_owner. For well-known names: old_owner
            # is the unique name that released the name. Use old_owner for correctness.
            if self.manager:
                self.manager.on_owner_disconnected(old_owner)

    def _on_position(self, position: Dict[str, Any]):
        """Called when lomiri sends a position update (~1Hz)."""
        # Merge heading if available
        if self._last_heading is not None:
            position['heading'] = self._last_heading

        self._last_position = position
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
