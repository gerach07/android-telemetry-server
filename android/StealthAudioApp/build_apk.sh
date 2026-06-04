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
  "$ROOT/android/StealthAudioApp/src/main/java/com/stealthaudio/StealthAudio.java" \
  "$ROOT/android/StealthAudioApp/src/main/java/com/stealthaudio/StealthAudioActivity.java"

cd "$OUT_DIR/classes"
jar cf "$OUT_DIR/classes.jar" .
cd "$ROOT"

"$BUILD_TOOLS/d8" --release --lib "$PLATFORM/android.jar" --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar"

cp "$ROOT/android/StealthAudioApp/src/main/AndroidManifest.xml" "$OUT_DIR/apk/AndroidManifest.xml"
cd "$OUT_DIR/apk"
"$BUILD_TOOLS/aapt" package -f -M AndroidManifest.xml -I "$PLATFORM/android.jar" -F "$OUT_DIR/unsigned.apk"
cp "$OUT_DIR/dex/classes.dex" "$OUT_DIR/apk/classes.dex"
zip -u "$OUT_DIR/unsigned.apk" "$OUT_DIR/apk/classes.dex"

"$BUILD_TOOLS/apksigner" sign \
  --ks "$OUT_DIR/debug.keystore" \
  --ks-key-alias androiddebugkey \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$APK_DIR/StealthAudio.apk" \
  "$OUT_DIR/unsigned.apk"

echo "Built APK: $APK_DIR/StealthAudio.apk"
