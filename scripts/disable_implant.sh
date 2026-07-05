#!/bin/bash
set -euo pipefail

echo "Disabling and completely uninstalling the Android telemetry implant..."
adb root
adb remount

# 1. Stop the running services
adb shell "stop system_telemetry_service 2>/dev/null || true"
adb shell "killall reporter 2>/dev/null || true"

# 2. Set the disable flag just in case
adb shell 'mkdir -p /data/local/tmp
  touch /data/local/tmp/reporter_disable
  chmod 666 /data/local/tmp/reporter_disable 2>/dev/null || true
  chown system:system /data/local/tmp/reporter_disable 2>/dev/null || true
  chcon u:object_r:system_data_file:s0 /data/local/tmp/reporter_disable 2>/dev/null || true'

# 3. Remove the core reporter binary and init script
adb shell "rm -f /system/bin/reporter"
adb shell "rm -f /system/etc/init/reporter.rc"

# 3b. Remove the updater helper and the privileged-permissions whitelist
adb shell "rm -f /system/bin/updater"
adb shell "rm -f /system/etc/permissions/privapp-permissions-stealth.xml"
adb shell "rm -f /data/local/tmp/updater.bin"

# 4. Remove all stealth system apps — clean up BOTH label-named and package-named folders.
# FIX S-2: install_system_apps_no_reboot.sh creates package-named dirs (com.stealthaudio/)
# but the old script only tried to remove label-named dirs (StealthAudio/), leaving APKs behind.
declare -A PKG_NAMES=(
  [StealthAudio]=com.stealthaudio
  [StealthAlert]=com.stealthalert
  [StealthGps]=com.stealthgps
  [StealthMonitor]=com.stealthmonitor
  [StealthSelfie]=com.stealthselfie
)
for app in StealthAudio StealthAlert StealthGps StealthMonitor StealthSelfie; do
  pkg="${PKG_NAMES[$app]}"
  for target in /system_ext/priv-app /system/priv-app; do
    adb shell "rm -rf '$target/$app' 2>/dev/null || true"
    adb shell "rm -rf '$target/$pkg' 2>/dev/null || true"
  done
done

# 5. Remove temporary files (FIX S-2/S-3: also remove implant.key on full wipe)
adb shell "rm -f /data/local/tmp/c2_url.txt"
adb shell "rm -f /data/local/tmp/coords.txt"
adb shell "rm -f /data/local/tmp/reporter.pid"
adb shell "rm -f /data/local/tmp/location_enabled"
adb shell "rm -f /data/local/tmp/ping_interval.txt"
adb shell "rm -f /data/local/tmp/implant.key"    # FIX S-3
adb shell "rm -f /data/system/ping_interval.txt"

echo "Implant and all associated stealth apps have been permanently removed."
echo "You may need to reboot the device to fully clear the apps from memory."
