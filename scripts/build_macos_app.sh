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
SIGN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/kalshi-model-sign.XXXXXX")"
VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/kalshi-model-verify.XXXXXX")"
ICONSET="$ICON_ROOT/KalshiModel.iconset"
ICON_FILE="build/KalshiModel.icns"
mkdir -p "$ICONSET" build dist
trap 'rm -rf "$ICON_ROOT" "$SIGN_ROOT" "$VERIFY_ROOT"' EXIT

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
xattr -cr "$ICONSET"
if ! iconutil -c icns "$ICONSET" -o "$ICON_FILE"; then
  if [[ ! -f "$ICON_FILE" ]]; then
    echo "Unable to create the macOS icon and no previous icon is available." >&2
    exit 1
  fi
  echo "Icon conversion was blocked by macOS metadata; reusing $ICON_FILE."
fi

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

APP_BUNDLE="dist/Kalshi Model.app"
SIGNED_BUNDLE="$SIGN_ROOT/Kalshi Model.app"

# Sign outside file-provider folders, which may attach metadata rejected by codesign.
ditto --norsrc "$APP_BUNDLE" "$SIGNED_BUNDLE"
xattr -cr "$SIGNED_BUNDLE"
codesign --force --deep --sign - "$SIGNED_BUNDLE"
codesign --verify --deep --strict "$SIGNED_BUNDLE"

mv "$APP_BUNDLE" "$SIGN_ROOT/unsigned.app"

ARCHIVE="dist/Kalshi-Model-macOS-$(uname -m).zip"
rm -f "$ARCHIVE"
ditto -c -k --sequesterRsrc --keepParent "$SIGNED_BUNDLE" "$ARCHIVE"
ditto -x -k "$ARCHIVE" "$VERIFY_ROOT"
codesign --verify --deep --strict "$VERIFY_ROOT/Kalshi Model.app"

echo "Signed archive: $PROJECT_DIR/$ARCHIVE"
