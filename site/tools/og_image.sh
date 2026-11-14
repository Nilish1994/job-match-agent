#!/bin/sh
# Render site/tools/og-image.html to site/assets/og-image.png (1200x630) with headless Chrome.
# Run from the repo root. Set CHROME if Chrome isn't at the macOS default path.
set -e
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PROFILE="$(mktemp -d)"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$PROFILE" \
  --window-size=1200,630 --force-device-scale-factor=1 --virtual-time-budget=2000 \
  --screenshot="$PWD/site/assets/og-image.png" "file://$PWD/site/tools/og-image.html"
rm -rf "$PROFILE"
