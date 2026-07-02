#!/bin/bash
set -euo pipefail

echo "Resuming the Android telemetry implant..."
adb root
adb remount

# 1. Remove the disable flag
adb shell "rm -f /data/local/tmp/reporter_disable"

# 2. Start the service
adb shell "start system_telemetry_service"

echo "Implant has been resumed and should be reporting."
