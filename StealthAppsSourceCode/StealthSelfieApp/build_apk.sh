#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
PLATFORM="${SDK_ROOT}/platforms/android-34"
BUILD_TOOLS="${SDK_ROOT}/build-tools/36.1.0"
OUT_DIR="$ROOT/build/stealthselfiebuild"
APK_DIR="$ROOT/system/priv-app/StealthSelfie"

mkdir -p "$OUT_DIR/classes" "$OUT_DIR/dex" "$OUT_DIR/apk" "$APK_DIR"

javac \
  -source 1.8 -target 1.8 \
  -bootclasspath "$PLATFORM/android.jar" \
  -classpath "$PLATFORM/android.jar" \
  -d "$OUT_DIR/classes" \
  "$ROOT/StealthAppsSourceCode/StealthSelfieApp/src/main/java/com/stealthselfie/AdminReceiver.java" \
  "$ROOT/StealthAppsSourceCode/StealthSelfieApp/src/main/java/com/stealthselfie/MainActivity.java"

cd "$OUT_DIR/classes"
jar cf "$OUT_DIR/classes.jar" .
cd "$ROOT"

"$BUILD_TOOLS/d8" --release --lib "$PLATFORM/android.jar" --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar"

cp "$ROOT/StealthAppsSourceCode/StealthSelfieApp/src/main/AndroidManifest.xml" "$OUT_DIR/apk/AndroidManifest.xml"
# Copy app resources (including launcher icon) into apk res tree
rm -rf "$OUT_DIR/apk/res"
mkdir -p "$OUT_DIR/apk/res"
cp -r "$ROOT/StealthAppsSourceCode/StealthSelfieApp/src/main/res/." "$OUT_DIR/apk/res/"

cd "$OUT_DIR/apk"
"$BUILD_TOOLS/aapt" package -f -M AndroidManifest.xml -S res -I "$PLATFORM/android.jar" -F "$OUT_DIR/unsigned.apk"
cp "$OUT_DIR/dex/classes.dex" "$OUT_DIR/apk/classes.dex"
cd "$OUT_DIR/apk"
zip -u "$OUT_DIR/unsigned.apk" classes.dex
cd "$ROOT"

"$BUILD_TOOLS/apksigner" sign \
  --key "$ROOT/keys/platform.pk8" \
  --cert "$ROOT/keys/platform.x509.pem" \
  --out "$APK_DIR/StealthSelfie.apk" \
  "$OUT_DIR/unsigned.apk"

echo "Built APK: $APK_DIR/StealthSelfie.apk"
