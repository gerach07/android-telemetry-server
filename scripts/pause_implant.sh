#!/bin/bash
set -euo pipefail

echo "Pausing the Android telemetry implant..."
adb root
adb remount

# 1. Stop the running services
adb shell "stop system_telemetry_service 2>/dev/null || true"
adb shell "killall reporter 2>/dev/null || true"

# 2. Set the disable flag so it won't run if the device reboots
adb shell "touch /data/local/tmp/reporter_disable"

echo "Implant has been paused. It will not run, even after a reboot."
echo "Use 'resume_implant.sh' to start it again."
