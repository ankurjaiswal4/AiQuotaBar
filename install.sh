#!/usr/bin/env bash
# install.sh: build and install the AiQuotaBar menu bar app.
#
#   ./install.sh              # build ~/Applications/AiQuotaBar.app and start it
#   ./install.sh --login      # also start AiQuotaBar when you log in
#   ./install.sh --uninstall  # quit and remove the app and its login item
#
# One line, no clone (downloads the source files from GitHub):
#   curl -fsSL https://raw.githubusercontent.com/ankurjaiswal4/AiQuotaBar/main/install.sh | bash
#
# The app is compiled on this Mac with swiftc (from the Xcode Command Line
# Tools, which also provide python3). An app built locally is not quarantined,
# so macOS opens it without "unidentified developer" warnings. Safe to re-run.
set -euo pipefail

ADD_LOGIN_ITEM="no"
UNINSTALL="no"
for arg in "$@"; do
  case "$arg" in
    --login) ADD_LOGIN_ITEM="yes" ;;
    --uninstall) UNINSTALL="yes" ;;
    *)
      echo "error: unknown argument '$arg' (use --login or --uninstall)" >&2
      exit 1
      ;;
  esac
done

APP_NAME="AiQuotaBar"
BUNDLE_ID="io.github.ankurjaiswal4.aiquotabar"
VERSION="1.0"
APP="$HOME/Applications/$APP_NAME.app"
ENGINE="aiquotabar.py"
UI_SOURCE="AiQuotaBar.swift"
LOGO="assets/logo.svg"
LOGIN_AGENT="$HOME/Library/LaunchAgents/local.aiquotabar.login.plist"
RAW_BASE="https://raw.githubusercontent.com/ankurjaiswal4/AiQuotaBar/main"

# Resolve the folder this script is in (empty when piped from curl).
SCRIPT_SRC="${BASH_SOURCE[0]:-}"
while [ -n "$SCRIPT_SRC" ] && [ -h "$SCRIPT_SRC" ]; do
  DIR="$(cd -P "$(dirname "$SCRIPT_SRC")" >/dev/null 2>&1 && pwd)"
  SCRIPT_SRC="$(readlink "$SCRIPT_SRC")"
  [[ "$SCRIPT_SRC" != /* ]] && SCRIPT_SRC="$DIR/$SCRIPT_SRC"
done
REPO_DIR="$(cd -P "$(dirname "${SCRIPT_SRC:-.}")" >/dev/null 2>&1 && pwd)"

quit_app() {
  if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    osascript -e "quit app id \"$BUNDLE_ID\"" >/dev/null 2>&1 || pkill -x "$APP_NAME" || true
    sleep 1
  fi
}

write_login_agent() {
  mkdir -p "$(dirname "$LOGIN_AGENT")"
  cat > "$LOGIN_AGENT" <<AGENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.aiquotabar.login</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-b</string><string>$BUNDLE_ID</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
AGENT
}

# Turns the SVG logo into AppIcon.icns. sips renders SVG at any size; iconutil
# packs the set. Optional: if either step fails the app keeps the generic icon.
build_icon() {
  local svg="$1" out="$2" set="$WORK_DIR/AppIcon.iconset" size
  [ -f "$svg" ] || return 1
  mkdir -p "$set"
  for size in 16 32 128 256 512; do
    sips -s format png -z "$size" "$size" "$svg" --out "$set/icon_${size}x${size}.png" >/dev/null 2>&1 || return 1
    sips -s format png -z "$((size * 2))" "$((size * 2))" "$svg" --out "$set/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || return 1
  done
  iconutil -c icns "$set" -o "$out" >/dev/null 2>&1
}

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
if [ "$UNINSTALL" = "yes" ]; then
  quit_app
  rm -rf "$APP"
  rm -f "$LOGIN_AGENT"
  echo "✓ Uninstalled $APP_NAME."
  echo "  Your settings remain in ~/.aiquotabar. Delete that folder to erase them."
  exit 0
fi

# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
if [ "$macos_major" -lt 13 ]; then
  echo "error: AiQuotaBar needs macOS 13 or later (this Mac runs $(sw_vers -productVersion))." >&2
  exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1 || [ ! -x /usr/bin/python3 ] || ! /usr/bin/python3 -c "" 2>/dev/null; then
  echo "error: the Xcode Command Line Tools are needed (they provide swiftc and python3)." >&2
  echo "Run:  xcode-select --install   then run this installer again." >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Sources: next to this script, or downloaded when piped from curl
# --------------------------------------------------------------------------- #
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
if [ -f "$REPO_DIR/$ENGINE" ] && [ -f "$REPO_DIR/$UI_SOURCE" ]; then
  SRC_DIR="$REPO_DIR"
else
  echo "Downloading AiQuotaBar..."
  curl -fsSL "$RAW_BASE/$ENGINE" -o "$WORK_DIR/$ENGINE"
  curl -fsSL "$RAW_BASE/$UI_SOURCE" -o "$WORK_DIR/$UI_SOURCE"
  mkdir -p "$WORK_DIR/assets"
  curl -fsSL "$RAW_BASE/$LOGO" -o "$WORK_DIR/$LOGO" || true  # the icon is optional
  # A truncated file or an HTML error page must never be built or installed.
  if ! head -n 1 "$WORK_DIR/$ENGINE" | grep -q '^#!/usr/bin/env python3' \
      || ! grep -q 'MenuBarExtra' "$WORK_DIR/$UI_SOURCE"; then
    echo "error: the download doesn't look like AiQuotaBar; nothing was installed." >&2
    exit 1
  fi
  SRC_DIR="$WORK_DIR"
fi

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
echo "Building $APP_NAME (about 10 seconds)..."
swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13" \
  "$SRC_DIR/$UI_SOURCE" -o "$WORK_DIR/$APP_NAME"

# --------------------------------------------------------------------------- #
# Install the app bundle
# --------------------------------------------------------------------------- #
quit_app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$WORK_DIR/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"
cp "$SRC_DIR/$ENGINE" "$APP/Contents/Resources/$ENGINE"
ICON_KEY=""
if build_icon "$SRC_DIR/$LOGO" "$APP/Contents/Resources/AppIcon.icns"; then
  ICON_KEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
fi
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  $ICON_KEY
</dict>
</plist>
PLIST
# Sign the whole bundle locally (ad hoc), so macOS ties the plist and the engine
# to the executable. No Apple account is involved.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
mdimport "$APP" >/dev/null 2>&1 || true  # Spotlight finds it now, not "eventually"

# Launch at login: written with --login, and refreshed if it was already on.
if [ "$ADD_LOGIN_ITEM" = "yes" ] || [ -f "$LOGIN_AGENT" ]; then
  write_login_agent
fi

open "$APP"

echo "✓ Installed $APP_NAME $VERSION."
echo "  app: $APP  (search \"$APP_NAME\" in Spotlight to reopen it)"
if [ -f "$LOGIN_AGENT" ]; then
  echo "  starts at login: yes"
else
  echo "  starts at login: no  (turn on \"Launch at login\" in its panel)"
fi
echo
echo "The meter appears in your menu bar in a few seconds."
echo "On first run, macOS asks to let python3 read the Claude Code Keychain item"
echo "(click Always Allow) and asks whether AiQuotaBar may send notifications (click Allow)."
