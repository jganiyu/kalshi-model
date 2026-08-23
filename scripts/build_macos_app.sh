#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
cd "$PROJECT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt

ICON_SOURCE="app/static/icon-1024.png"
ICON_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/kalshi-model-icon.XXXXXX")"
ICONSET="$ICON_ROOT/KalshiModel.iconset"
ICON_FILE="build/KalshiModel.icns"
mkdir -p "$ICONSET" build dist
trap 'rm -rf "$ICON_ROOT"' EXIT

sips -z 16 16 "$ICON_SOURCE" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$ICON_SOURCE" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ICON_SOURCE" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_512x512.png" >/dev/null
cp "$ICON_SOURCE" "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "$ICON_FILE"

.venv/bin/python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "Kalshi Model" \
  --icon "$ICON_FILE" \
  --osx-bundle-identifier "com.jganiyu.kalshimodel" \
  --hidden-import "app.main" \
  --add-data "app/templates:app/templates" \
  --add-data "app/static:app/static" \
  app/__main__.py

codesign --force --deep --sign - "dist/Kalshi Model.app"

ARCHIVE="dist/Kalshi-Model-macOS-$(uname -m).zip"
rm -f "$ARCHIVE"
ditto -c -k --sequesterRsrc --keepParent "dist/Kalshi Model.app" "$ARCHIVE"

echo "Built: $PROJECT_DIR/dist/Kalshi Model.app"
echo "Archive: $PROJECT_DIR/$ARCHIVE"
