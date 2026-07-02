#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

APPS=(StealthAudio StealthAlert StealthGps StealthMonitor StealthSelfie)

declare -A PACKAGE_NAMES=(
  [StealthAudio]=com.stealthaudio
  [StealthAlert]=com.stealthalert
  [StealthGps]=com.stealthgps
  [StealthMonitor]=com.stealthmonitor
  [StealthSelfie]=com.stealthselfie
)

usage() {
  echo "Usage: $0 [StealthAudio StealthAlert StealthGps StealthMonitor StealthSelfie ...]"
  echo "If no app names are provided, all system apps are reinstalled."
}

SELECTED_APPS=()
if [ "$#" -eq 0 ]; then
  SELECTED_APPS=("${APPS[@]}")
else
  for app in "$@"; do
    case " ${APPS[*]} " in
      *" $app "*)
        SELECTED_APPS+=("$app")
        ;;
      *)
        echo "Unknown app: $app" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
fi

echo "Installing Stealth system apps..."
adb root
adb remount
adb shell mkdir -p /system/etc/init /system/etc/permissions

# ── Push APKs to package-named folders in /system/priv-app/ ──
# PackageManager expects the folder name to match the package name.
# We push ONLY to /system/priv-app/ (not system_ext) to avoid duplicates.
for app in "${SELECTED_APPS[@]}"; do
  package_name="${PACKAGE_NAMES[$app]}"
  echo "Pushing $app → /system/priv-app/$package_name/"

  # Remove any old folder with the app's build name (e.g. StealthAudio/)
  adb shell "rm -rf /system/priv-app/$app" 2>/dev/null || true
  adb shell "rm -rf /system_ext/priv-app/$app" 2>/dev/null || true
  adb shell "rm -rf /system_ext/priv-app/$package_name" 2>/dev/null || true

  # Create the package-named folder and push the APK
  adb shell "mkdir -p /system/priv-app/$package_name"
  adb push "$ROOT/system/priv-app/$app/$app.apk" "/system/priv-app/$package_name/$package_name.apk"

  # Fix permissions: directory 755, APK 644
  adb shell "chmod 755 /system/priv-app/$package_name"
  adb shell "chmod 644 /system/priv-app/$package_name/$package_name.apk"

  # Delete any .idsig files — they break APK Signature Scheme v4 verification
  adb shell "rm -f /system/priv-app/$package_name/*.idsig" 2>/dev/null || true

  # Restore SELinux contexts
  adb shell "restorecon -R /system/priv-app/$package_name 2>/dev/null || true"
done

echo "Pushing privileged permissions whitelist..."
adb push "$ROOT/privapp-permissions-stealth.xml" /system/etc/permissions/privapp-permissions-stealth.xml
adb shell chmod 644 /system/etc/permissions/privapp-permissions-stealth.xml
adb shell "restorecon -v /system/etc/permissions/privapp-permissions-stealth.xml 2>/dev/null || true"

echo "Pushing reporter binary and reporter.rc..."
adb push "$ROOT/system/bin/reporter" /system/bin/reporter
adb push "$ROOT/system/etc/init/reporter.rc" /system/etc/init/reporter.rc
adb shell chmod 755 /system/bin/reporter
adb shell chmod 644 /system/etc/init/reporter.rc
adb shell "restorecon -v /system/bin/reporter /system/etc/init/reporter.rc 2>/dev/null || true"

# Pre-create the GPS handoff files so StealthGps can write them without
# needing directory write access under /data/local/tmp.
adb shell "touch /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled"
adb shell "chmod 666 /data/local/tmp/coords.txt /data/local/tmp/gps_errors.txt /data/local/tmp/location_enabled"

# Migrate ping_interval.txt from old path to new path if needed
adb shell "[ -f /data/local/tmp/ping_interval.txt ] && cp /data/local/tmp/ping_interval.txt /data/system/ping_interval.txt && chmod 644 /data/system/ping_interval.txt || true"

# Restore full SELinux contexts on the priv-app directories
adb shell "restorecon -R /system/priv-app/ 2>/dev/null || true"

echo "Restarting Android framework to reload system apps..."
adb shell "stop && start" || true

# If direct stop/start fails, try zygote restart as a fallback.
adb shell "setprop ctl.restart zygote" || true

echo "Verifying installed packages..."
sleep 10
for app in "${SELECTED_APPS[@]}"; do
  package_name="${PACKAGE_NAMES[$app]}"
  if adb shell pm path "$package_name" >/dev/null 2>&1; then
    echo "  ✓ $app ($package_name)"
  else
    echo "  ✗ $app ($package_name) — NOT registered by PackageManager" >&2
  fi
done

echo ""
echo "Reporter binary:  $(adb shell ls -la /system/bin/reporter 2>/dev/null | awk '{print $5, $6, $7}')"
echo "Reporter init.rc: $(adb shell ls -la /system/etc/init/reporter.rc 2>/dev/null | awk '{print $5, $6, $7}')"
echo "Reporter PID:     $(adb shell pidof reporter 2>/dev/null || echo 'NOT RUNNING')"
echo ""
echo "Done. If apps don't appear, reboot the device: adb reboot"