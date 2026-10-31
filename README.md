# GeoClue2 Bridge - GPS for Firefox on Ubuntu Touch

Provides GPS location to Firefox/LibreWolf via the standard GeoClue2 D-Bus API,
bridging to Ubuntu Touch's `lomiri-location-service`.

## Quick Start

```bash
# Deploy to device (will ask for device sudo password)
./deploy.sh install

# Check it's running
./deploy.sh status

# Test the API
./deploy.sh test
```

## Firefox Setup

After deployment, a `user.js` file is deployed to Firefox's profile with the
required geolocation preferences:

```javascript
// Enable GeoClue2 for GPS location via geoclue2-bridge
user_pref("geo.provider.use_geoclue", true);
// Disable Mozilla Location Service provider
user_pref("geo.provider.use_mls", false);
// Disable MLS fallback timer (see "Position Jumping" section below)
user_pref("geo.provider.geoclue.mls_fallback_timeout_ms", 0);
```

If the user.js is missing, create it manually at:
`~/.local/share/firefox.fuseteam/.mozilla/firefox/<profile>/user.js`

**Important:** Firefox (`firefox.fuseteam`) does NOT have the Ubuntu Touch `location`
policy group by default. You can add it properly via AppArmor.

All `add-location-policy.sh` commands must run **on the device** (via SSH), not on the host:

```bash
# List apps and their location status (no sudo needed):
ssh phablet@192.168.1.120 "bash /home/phablet/geoclue2-bridge/add-location-policy.sh --list"

# Add location permission to Firefox (needs sudo — use -t for password prompt):
ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/add-location-policy.sh firefox.fuseteam_navigator_150.0.2"

# Remove it:
ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/add-location-policy.sh --remove firefox.fuseteam_navigator_150.0.2"
```

> **Note:** Use `ssh -t` (allocates a terminal) for any command requiring `sudo`.
> Without `-t`, sudo cannot prompt for the password.

This modifies Firefox's click AppArmor policy and regenerates its profile, giving it
proper system-level location access. Alternatively, use the whitelist (simpler but
doesn't grant D-Bus level access to lomiri-location-service directly):

```bash
ssh phablet@192.168.1.120 "echo 'firefox.fuseteam_navigator_150.0.2' >> /home/phablet/geoclue2-bridge/location-whitelist.txt"
ssh -t phablet@192.168.1.120 "sudo systemctl restart geoclue2-bridge"
```

uwolf (LibreWolf) already has the `location` permission and is unconfined, so it
works without any changes.

### AppArmor GeoClue2 Patch (Required for Firefox)

Ubuntu Touch's `location` policy group only grants D-Bus access to
`com.lomiri.location.Service`, not `org.freedesktop.GeoClue2`. Firefox needs the
profile patched to talk to our bridge:

```bash
ssh -t phablet@192.168.1.120 "sudo /home/phablet/geoclue2-bridge/patch-apparmor-geoclue2.sh"
```

> **⚠️ Must be re-run after Firefox updates** (the click install regenerates the profile).

Then restart Firefox for the new AppArmor rules to take effect.

### After a System or Firefox Update

Only one step is needed (auto-detects the newest installed Firefox):

```bash
ssh -t phablet@192.168.1.120 "sudo ~/geoclue2-bridge/gps.sh"
```

Then restart Firefox. The bridge itself lives in `/home` and `/etc/systemd/system`,
which survive system updates. The old `gps-fix` library patch is no longer needed
(the deadlock is fixed upstream).

### Testing

```bash
ssh phablet@192.168.1.120 "python3 ~/geoclue2-bridge/test-client.py 60"
```

Starts a GeoClue2 client, waits up to 60s for a fix, stops it, and checks the bridge
survived GPS shutdown. Exit code: 0 = fix, 2 = no fix (indoors?), 1 = bridge crashed.
Cached "last known" positions are printed as `CACHED` and don't count as a fix.

Off-device (or on-device, no root) test against a fake lomiri-location-service:

```bash
dbus-run-session -- python3 geoclue-bridge/test-bridge-mock.py
```

## Startup Timing and the Last-Fix Cache

GeoClue2 has no timeout of its own, but clients do:
- **Firefox** falls back to network geolocation (Google API) if GeoClue sends nothing
  within 5 s of `Start()` (`geo.provider.geoclue.mls_fallback_timeout_ms` = 5000).
- **Web pages** pick their own `getCurrentPosition()` timeout (default: none). Common
  values are 5–10 s (Leaflet's `locate()` uses 10 s), after which the page shows an error.
- A GPS hot start (GPS used minutes ago) takes a few seconds; cold starts take 30 s+.

So the bridge remembers the last fix (in memory and in
`~/.cache/geoclue2-bridge/last-fix.json`, mode 600). If GPS hasn't produced a fix 3 s
after a client's `Start()`, the client gets the last known fix if it's under 1 h old.
Its accuracy is increased by 1.5 m/s of age, and its `Description` says
"Last known position (N s old)". Real fixes replace it as soon as they arrive.
The accuracy is the only staleness signal that reaches web pages, because Firefox
ignores GeoClue's `Timestamp` and stamps each location with the current time.
If GPS is already running, a newly started client gets the current fix immediately.

## How It Works

```
Firefox ──(GeoClue2 D-Bus)──> geoclue2-bridge.py ──(lomiri D-Bus)──> lomiri-location-service ──> GPS
```

The bridge:
1. Registers `org.freedesktop.GeoClue2` on the system D-Bus
2. Implements Manager/Client/Location interfaces (GeoClue2 standard)
3. **Checks caller's AppArmor confinement** - only apps with `location` permission can access
4. Creates a session with `lomiri-location-service` to receive GPS updates
5. Forwards position data to authorized GeoClue2 clients

## Access Control

The bridge respects Ubuntu Touch confinement. Access is granted to:
- **Unconfined apps** (uwolf/LibreWolf, terminal, devtools)
- **Apps with `location` policy group** (pure-maps, navigator, morph-browser, etc.)
- **Apps in the whitelist** (`/home/phablet/geoclue2-bridge/location-whitelist.txt`)

Denied apps get `org.freedesktop.GeoClue2.Error.PermissionDenied`.

## Requirements

- Ubuntu Touch device with `lomiri-location-service` running
- SSH access: `phablet@192.168.1.120` (no password for SSH)
- Sudo access on device (one-time setup, needs password)
- GPS fix requires outdoor/window position for satellite visibility

## Troubleshooting

**Position "jumping" to a wrong location (Firefox MLS Fallback Bug):**

Firefox's `GeoclueLocationProvider` (in `dom/system/linux/GeoclueLocationProvider.cpp`)
has a built-in MLS (Mozilla Location Service) fallback mechanism:

1. When Firefox calls `Start()` on the GeoClue2 client, it starts a timer
   (default: 5000ms, controlled by `geo.provider.geoclue.mls_fallback_timeout_ms`)
2. If GPS doesn't provide a position before the timer fires, Firefox starts the
   `NetworkGeolocationProvider` which queries `geo.provider.network.url` for
   IP-based geolocation
3. **Bug:** Once MLS starts, `UpdateLastPosition()` only cancels the *timer* but
   does NOT call `StopMLSFallback()` — so the MLS provider continues running
   indefinitely, providing periodic (~5s) IP-based positions alongside GPS data
4. Result: the map oscillates between the correct GPS position and a wrong
   IP-geolocation position (which can be hundreds of km away)

**Fix:** Set `geo.provider.geoclue.mls_fallback_timeout_ms` to `0` in user.js.
This disables the fallback timer entirely, so MLS never starts.

**Context:** Mozilla shut down MLS for third-party use in June 2024. The fallback
now uses stale cached results or Google's geolocation API, producing wrong
positions. This is a known issue across the Linux desktop ecosystem:
- [Fedora discussion](https://discussion.fedoraproject.org/t/article-series-proposal-the-state-of-geoclue-and-mozilla-location-service-on-fedora-in-2025/141490)
- [Void Linux issue](https://github.com/void-linux/void-packages/issues/52823)
- Source: `dom/system/linux/GeoclueLocationProvider.cpp` in Firefox

**No position in Firefox:**
- Check bridge is running: `systemctl status geoclue2-bridge`
- Check logs: `journalctl -u geoclue2-bridge -f`
- Ensure GPS is enabled: `lomiri-location-serviced-cli --bus system --property state --get`
- Ensure device has GPS visibility (outdoors/near window)
- Verify `geo.provider.use_geoclue = true` in Firefox `about:config`

**Bridge fails to start:**
- D-Bus policy must be installed: check `/etc/dbus-1/system.d/org.freedesktop.GeoClue2.conf`
- If missing, re-run: `./deploy.sh install`

**Permission denied / trust store:**
- The lomiri trust store may prompt (on the phone screen) the first time
- Check indicator-location is enabled in system settings

## Uninstall

```bash
./deploy.sh uninstall
```

## Technical Notes

- The bridge runs as user `phablet` via systemd (`geoclue2-bridge.service`)
- D-Bus policy file at `/etc/dbus-1/system.d/org.freedesktop.GeoClue2.conf`
- Systemd unit at `/etc/systemd/system/geoclue2-bridge.service`
- Bridge script at `/home/phablet/geoclue2-bridge/geoclue2-bridge.py`
- lomiri-location-service uses a non-standard callback protocol (see AGENTS.md)
