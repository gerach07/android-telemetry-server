#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
PLATFORM="${SDK_ROOT}/platforms/android-34"
BUILD_TOOLS="${SDK_ROOT}/build-tools/36.1.0"
OUT_DIR="$ROOT/build/stealthaudiobuild"
APK_DIR="$ROOT/system/priv-app/StealthAudio"

mkdir -p "$OUT_DIR/classes" "$OUT_DIR/dex" "$OUT_DIR/apk" "$APK_DIR"

if [ ! -f "$OUT_DIR/debug.keystore" ]; then
  keytool -genkeypair -v \
    -keystore "$OUT_DIR/debug.keystore" \
    -storepass android -keypass android \
    -alias androiddebugkey \
    -dname "CN=Android Debug,O=Android,C=US" \
    -keyalg RSA -keysize 2048 -validity 10000
fi

javac \
  -source 1.8 -target 1.8 \
  -bootclasspath "$PLATFORM/android.jar" \
  -classpath "$PLATFORM/android.jar" \
  -d "$OUT_DIR/classes" \
  $(find "$ROOT/StealthAppsSourceCode/StealthAudioApp/src/main/java" -name "*.java" | tr '\n' ' ')

cd "$OUT_DIR/classes"
jar cf "$OUT_DIR/classes.jar" .
cd "$ROOT"

"$BUILD_TOOLS/d8" --release --lib "$PLATFORM/android.jar" --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar"

cp "$ROOT/StealthAppsSourceCode/StealthAudioApp/src/main/AndroidManifest.xml" "$OUT_DIR/apk/AndroidManifest.xml"
cd "$OUT_DIR/apk"
"$BUILD_TOOLS/aapt" package -f -M AndroidManifest.xml -S "$ROOT/StealthAppsSourceCode/StealthAudioApp/src/main/res" -I "$PLATFORM/android.jar" -F "$OUT_DIR/unsigned.apk"
cp "$OUT_DIR/dex/classes.dex" "$OUT_DIR/apk/classes.dex"
cd "$OUT_DIR/apk"
zip -u "$OUT_DIR/unsigned.apk" classes.dex
cd "$ROOT"

"$BUILD_TOOLS/apksigner" sign \
  --key "$ROOT/keys/platform.pk8" \
  --cert "$ROOT/keys/platform.x509.pem" \
  --out "$APK_DIR/StealthAudio.apk" \
  "$OUT_DIR/unsigned.apk"

echo "Built APK: $APK_DIR/StealthAudio.apk"
