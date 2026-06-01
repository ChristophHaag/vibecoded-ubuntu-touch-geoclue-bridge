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
./deploy.sh install    # Copies files, installs D-Bus policy (needs sudo), starts service
./deploy.sh status     # Check service status and logs
./deploy.sh test       # Verify GeoClue2 API is responding
./deploy.sh uninstall  # Remove everything
```

The install step will:
1. Copy bridge files to `/home/phablet/geoclue2-bridge/`
2. Install D-Bus policy to `/etc/dbus-1/system.d/` (requires remount rw, needs sudo)
3. Install systemd unit to `/etc/systemd/system/` (writable)
4. Enable and start the service

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
