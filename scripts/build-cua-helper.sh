#!/bin/sh
# The still-image ScreenCaptureKit API needs macOS 14 or newer.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source="$root/tessera/native/mac/TesseraBackground.swift"
identity=${TESSERA_CODESIGN_IDENTITY:--}
swiftc=${TESSERA_SWIFTC:-/usr/bin/swiftc}
bundle_id=${TESSERA_CUA_BUNDLE_ID:-app.tessera.cua}

if [ "$#" -ge 1 ]; then
  app=$1
elif [ -n "${TESSERA_CUA_APP:-}" ]; then
  app=$TESSERA_CUA_APP
elif [ -n "${TESSERA_HOME:-}" ]; then
  app=$TESSERA_HOME/cua/TesseraCUA.app
else
  echo "usage: bash scripts/build-cua-helper.sh <TesseraCUA.app>" >&2
  echo "or set TESSERA_CUA_APP or TESSERA_HOME" >&2
  exit 2
fi

if [ ! -f "$source" ]; then
  echo "missing helper source: $source" >&2
  exit 1
fi

if ! command -v "$swiftc" >/dev/null 2>&1; then
  echo "swiftc not found ($swiftc). Install the Xcode command line tools, then rerun this script." >&2
  exit 1
fi

if [ ! -x /usr/bin/codesign ]; then
  echo "codesign is not available. This script runs on macOS." >&2
  exit 1
fi

case $bundle_id in
  *[!A-Za-z0-9.-]*)
    echo "TESSERA_CUA_BUNDLE_ID must be letters, digits, dots, and hyphens" >&2
    exit 1
    ;;
esac

macos_min=${TESSERA_MACOS_TARGET:-14.0}
case $macos_min in
  ''|*[!0-9.]*)
    echo "TESSERA_MACOS_TARGET must look like 14.0" >&2
    exit 1
    ;;
esac
arch=$(uname -m)
contents=$app/Contents
mkdir -p "$contents/MacOS" "$contents/Resources"
"$swiftc" -O -target "$arch-apple-macos$macos_min" -framework ScreenCaptureKit -o "$contents/MacOS/TesseraCUA" "$source"

cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>$bundle_id</string>
  <key>CFBundleName</key>
  <string>TesseraCUA</string>
  <key>CFBundleExecutable</key>
  <string>TesseraCUA</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSAccessibilityUsageDescription</key>
  <string>Tessera computer use reads and acts on other apps when you allow it.</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>Tessera computer use captures one app window when you allow it.</string>
</dict>
</plist>
EOF

/usr/bin/codesign --force --sign "$identity" "$contents/MacOS/TesseraCUA"
/usr/bin/codesign --force --sign "$identity" "$app"
echo "built $app"
if [ "$identity" = "-" ]; then
  echo "Ad-hoc signature. If a tool reports that Screen Recording or Accessibility is not granted to TesseraCUA, enable TesseraCUA in System Settings > Privacy & Security." >&2
fi
