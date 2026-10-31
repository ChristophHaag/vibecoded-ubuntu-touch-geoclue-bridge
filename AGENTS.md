# GeoClue2 Bridge for Ubuntu Touch

## Overview

This bridge provides a **GeoClue2-compatible D-Bus service** (`org.freedesktop.GeoClue2`) that
bridges to Ubuntu Touch's `lomiri-location-service`, enabling Firefox/LibreWolf and other
desktop Linux applications to access GPS on Ubuntu Touch.

## Architecture

```
Firefox/LibreWolf → GeoClue2 D-Bus API (system bus) → geoclue2-bridge.py → lomiri-location-service
```

### Protocol Details

**lomiri-location-service** uses a non-standard D-Bus callback protocol:
- Service: `com.lomiri.location.Service` on system bus at `/com/lomiri/location/Service`
- Method: `CreateSessionForCriteria` takes flat-encoded Criteria (NOT `a{sv}` dict!)
- The Criteria encoding is: `bbbb d b b b` = 4 booleans (position, altitude, heading, velocity),
  1 double (horizontal accuracy meters), 3 booleans (has_vertical_acc, has_velocity_acc, has_heading_acc)
- After session creation, the SERVICE calls `UpdatePosition` METHOD on the CLIENT (reverse call pattern)
- Position encoding: `d d b[d] b[d] b[d] x` = lat, lon, has_alt[alt], has_hacc[hacc], has_vacc[vacc], timestamp_ns

**GeoClue2** standard D-Bus API:
- Bus name: `org.freedesktop.GeoClue2` on system bus
- Manager: `/org/freedesktop/GeoClue2/Manager` → `GetClient()` returns client path
- Client: properties (DesktopId, RequestedAccuracyLevel, Location), methods (Start, Stop), signal (LocationUpdated)
- Location: properties (Latitude, Longitude, Accuracy, Altitude, Speed, Heading, Timestamp)

## Requirements

- Device: Ubuntu Touch with lomiri-location-service running
- Python 3 with `dbus` and `gi` (GLib) modules (pre-installed on UT)
- Sudo access (one-time, for D-Bus policy installation)

## Access Control (Confinement)

The bridge enforces Ubuntu Touch app confinement. Only these apps can access location:

1. **Unconfined apps** (e.g., uwolf/LibreWolf, terminal) - always allowed
2. **Click apps with 'location' policy group** (e.g., pure-maps, navigator, morph-browser)
3. **Whitelisted apps** - listed in `/home/phablet/geoclue2-bridge/location-whitelist.txt`

Apps WITHOUT the location permission (like Firefox) will be rejected unless
added to the whitelist. To allow Firefox:

```bash
echo "firefox.fuseteam_navigator_150.0.2" >> /home/phablet/geoclue2-bridge/location-whitelist.txt
sudo systemctl restart geoclue2-bridge
```

The confinement check works by:
- Getting the caller's AppArmor label via `GetConnectionCredentials`
- Looking up the app's policy_groups in `/var/lib/apparmor/clicks/<app_id>.json`
- Checking if `"location"` is present

## Deployment

```bash
./deploy.sh install    # Copy files, then 'sudo gps.sh' on the device (one password prompt)
./deploy.sh copy       # Copy files only, no sudo (agents can do this themselves)
./deploy.sh status     # 'gps.sh --status' + recent bridge log, no sudo
./deploy.sh test       # Real fix test via test-client.py
./deploy.sh uninstall  # Remove service and D-Bus policy
```

`gps.sh` (on the device, as root) is the single idempotent setup step:
1. Runs `~/gps-hidl-workaround/gps-hidl-workaround.sh install` if deployed
   (deploy.sh copies it from the sibling `../gps-hidl-workaround/`)
2. Installs the D-Bus policy to `/etc/dbus-1/system.d/` if it differs
   (remounts / rw only then)
3. Installs/enables the systemd unit; restarts the bridge only if
   `geoclue2-bridge.py` or the unit changed. It records their md5 and the
   service start time in `/run/geoclue2-bridge.deployed`, because scp resets
   mtimes and would otherwise cause a restart on every deploy.
4. Firefox: `add-location-policy.sh` then `patch-apparmor-geoclue2.sh`
   (this order matters: the first regenerates profiles via aa-clickhook)

`gps.sh --status` works without root and is the quickest way to check the setup.

Older versions of `patch-apparmor-geoclue2.sh` used the marker text (which has
parentheses) unescaped in a regex. Removing the old block never matched, so
every run appended another copy of the rules. The current version strips all
copies and writes one.

## Firefox Configuration

In `about:config`, set:
```
geo.provider.use_geoclue = true
```

Firefox will then connect to `org.freedesktop.GeoClue2` on the system bus for location.

## Key Technical Findings

1. **lomiri-location-service does NOT use standard D-Bus introspection** - the interface XML is not exported
2. **Position updates are PUSHED via method calls on the client** - not via signals or PropertiesChanged
3. **CreateSessionForCriteria uses flat struct encoding** - not the `a{sv}` documented in older locationd code
4. **The D-Bus system bus policy file is on the read-only system partition** - requires `mount -o remount,rw /`
5. **GPS won't report indoors** - testing requires the device to have satellite visibility
6. **dbus-python message filters must be removed with the same object that was added** - a
   bound method (`self._message_filter`) is a new object on each access; removing a filter
   libdbus doesn't know triggers a check failure that **aborts** the process (uncatchable).
   Store the bound method once (`self._filter`) and use it for both add and remove.
7. **All lomiri calls are asynchronous** (`send_message_with_reply`). lomiri can hang (e.g.
   the GNSS HAL), and it calls UpdatePosition back on us, so a blocking call stalls every
   GeoClue2 client. UpdatePosition/UpdateHeading are only accepted from connections of
   the lomiri process (PID check): lomiri replies to CreateSessionForCriteria from one
   unique name but sends the callbacks from a **second connection**.
8. **`dbus.exceptions.DBusException(msg, name='org...Error')`**: the error name must be
   the `name=` keyword. Passed positionally it becomes the message, and callers see a
   generic Python error name.
9. **Firefox's GeoClue provider** (`dom/system/linux/GeoclueLocationProvider.cpp`)
   falls back to network geolocation after 5 s without a location, drops Heading unless
   Speed > 0, treats Altitude < -500 as unknown, and ignores `Timestamp` (uses now).
   That's why cached fixes are sent after 3 s with age-inflated accuracy.
10. **Clients are per-peer**: `GetClient` reuses the caller's client (like real GeoClue),
    `CreateClient` makes a new one. Only the owning peer may Start/Stop/Set a client or read
    its Location objects. Coordinates are logged at debug level only.

## Source References

- lomiri-location-service: https://gitlab.com/ubports/development/core/lomiri-location-service
- D-Bus API doc: ubports/locationd repo `docs/reference/dbus-api.md`
- Codec definitions: `include/location_service/com/lomiri/location/codec.h`
- Session skeleton: `src/location_service/com/lomiri/location/service/session/skeleton.cpp`
- GeoClue2 spec: https://www.freedesktop.org/software/geoclue/docs/

## Files

- `geoclue2-bridge.py` - Main bridge service (Python 3)
- `org.freedesktop.GeoClue2.conf` - D-Bus system bus policy
- `geoclue2-bridge.service` - systemd unit file
- `deploy.sh` - Deployment script
- `gps.sh` - Idempotent on-device setup/repair of everything; re-run after Firefox/system updates
- `test-client.py` - On-device GeoClue2 Start/fix/Stop test (verifies bridge survives GPS stop)
- `test-bridge-mock.py` - Bridge test against a fake lomiri on a private bus:
  `dbus-run-session -- python3 test-bridge-mock.py` (no root, runs on host or device)
