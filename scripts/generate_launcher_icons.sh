#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v convert >/dev/null 2>&1; then
  echo "ImageMagick 'convert' is required. Install it and re-run." >&2
  exit 2
fi

usage() {
  echo "Usage: $0 <app-path> <source-png>"
  echo "  <app-path> is relative to repo root, e.g. StealthAppsSourceCode/StealthMonitorApp"
  echo "  <source-png> is an existing high-res PNG to use as source (will be resized)."
}

if [ "$#" -ne 2 ]; then usage; exit 1; fi

APP_PATH="$1"
SRC="$2"

if [ ! -f "$ROOT/$APP_PATH/src/main/$SRC" ]; then
  echo "Source PNG not found: $ROOT/$APP_PATH/src/main/$SRC" >&2
  exit 1
fi

RES_DIR="$ROOT/$APP_PATH/src/main/res"

mkdir -p "$RES_DIR/mipmap-mdpi" "$RES_DIR/mipmap-hdpi" \
         "$RES_DIR/mipmap-xhdpi" "$RES_DIR/mipmap-xxhdpi" \
         "$RES_DIR/mipmap-xxxhdpi" "$RES_DIR/mipmap-anydpi-v26"

# Launcher sizes (px): mdpi 48, hdpi 72, xhdpi 96, xxhdpi 144, xxxhdpi 192
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize 48x48^ -gravity center -background transparent -extent 48x48 "$RES_DIR/mipmap-mdpi/ic_launcher.png"
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize 72x72^ -gravity center -background transparent -extent 72x72 "$RES_DIR/mipmap-hdpi/ic_launcher.png"
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize 96x96^ -gravity center -background transparent -extent 96x96 "$RES_DIR/mipmap-xhdpi/ic_launcher.png"
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize 144x144^ -gravity center -background transparent -extent 144x144 "$RES_DIR/mipmap-xxhdpi/ic_launcher.png"
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize 192x192^ -gravity center -background transparent -extent 192x192 "$RES_DIR/mipmap-xxxhdpi/ic_launcher.png"

# Also produce a high-res adaptive foreground (432x432) and place in anydpi mipmap
# Put the artwork inside the adaptive-icon safe area (~72%) to avoid launcher
# cropping/scaling artifacts. We'll resize the source to the safe size and
# center it on a 432x432 transparent canvas.
SAFE_PCT=0.72
CANVAS=432
SAFE_SIZE=$(printf "%.0f" $(echo "$CANVAS * $SAFE_PCT" | bc -l))
convert "$ROOT/$APP_PATH/src/main/$SRC" -resize ${SAFE_SIZE}x${SAFE_SIZE}^ -gravity center -background transparent -extent ${SAFE_SIZE}x${SAFE_SIZE} "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground_tmp.png"
convert -size ${CANVAS}x${CANVAS} xc:none "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground.png"
composite -gravity center "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground_tmp.png" "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground.png" "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground.png"
rm -f "$RES_DIR/mipmap-anydpi-v26/ic_launcher_foreground_tmp.png"

# Provide a round XML wrapper for launchers that expect ic_launcher_round
cat > "$RES_DIR/mipmap-anydpi-v26/ic_launcher_round.xml" <<'EOF'
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
  <background android:drawable="@color/ic_launcher_background" />
  <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
EOF

# Adaptive icon XML
cat > "$RES_DIR/mipmap-anydpi-v26/ic_launcher.xml" <<'EOF'
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
EOF

# Ensure a background color resource exists (fallback to white)
mkdir -p "$RES_DIR/values"
if [ ! -f "$RES_DIR/values/colors.xml" ]; then
  cat > "$RES_DIR/values/colors.xml" <<'EOF'
<resources>
  <color name="ic_launcher_background">#FFFFFFFF</color>
</resources>
EOF
fi

echo "Generated launcher icons for $APP_PATH -> $RES_DIR"
