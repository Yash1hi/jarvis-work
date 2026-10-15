#!/bin/sh
# Install (or reinstall) jarvisd and Jarvis Controls (its menu-bar on/off switch and cost counter)
# as login agents. If Jarvis was turned off from the menu bar, it stays off.
# Remove both with: launchd/install.sh --remove
#
# Run it from your normal shell: jarvisd (and the agents it starts) get this shell's PATH, so they
# find claude, codex, node, gh and the rest the same way you do.
set -e
cd "$(dirname "$0")/.."
mkdir -p .build logs
DOMAIN="gui/$(id -u)"
AGENTS="com.jarvis.jarvisd com.jarvis.menubar"

# Don't cut Yash off: wait (up to 3 min) while a conversation is live. --force skips the wait.
if [ "$1" != "--force" ] && [ "$1" != "--remove" ]; then
  for _ in $(seq 1 180); do [ -f logs/live.json ] || break; sleep 1; done
fi

stop() {  # stop <label> [process pattern]
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  # bootout returns before the old process is gone. Starting the new one too early fails with error 5,
  # or (for Jarvis Controls) leaves a blank menu-bar item, so wait for both launchd and the process.
  for _ in $(seq 1 20); do
    if ! launchctl print "$DOMAIN/$1" >/dev/null 2>&1 && { [ -z "$2" ] || ! pgrep -f "$2" >/dev/null; }; then break; fi
    sleep 0.5
  done
}

for label in $AGENTS; do
  case $label in
    com.jarvis.jarvisd) stop "$label" "python3 -m jarvisd" ;;
    com.jarvis.menubar) stop "$label" "Jarvis Controls.app/Contents/MacOS" ;;
  esac
  [ "$1" = "--remove" ] && rm -f ~/Library/LaunchAgents/$label.plist
done
if [ "$1" = "--remove" ]; then echo "jarvisd and Jarvis Controls removed"; exit 0; fi

# Build an app bundle: build_app <name> <bundle id> <source> [extra Info.plist keys]
build_app() {
  APP="$HOME/Applications/$1.app"
  if [ -x "$APP/Contents/MacOS/$1" ] && [ ! "$3" -nt "$APP/Contents/MacOS/$1" ]; then return; fi
  mkdir -p "$APP/Contents/MacOS"
  swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/$1" "$3"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$2</string>
  <key>CFBundleName</key><string>$1</string>
  <key>CFBundleExecutable</key><string>$1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  $4
</dict>
</plist>
PLIST
  codesign --force --sign - "$APP"
}

# Jarvis.app: the launcher macOS attributes the mic to (see jarvisd/launcher.swift).
# Rebuilding changes its signature, which can reset that permission, so it only rebuilds when changed.
build_app "Jarvis" com.jarvis.daemon jarvisd/launcher.swift "
  <key>NSMicrophoneUsageDescription</key><string>Jarvis listens for \"Hey Jarvis\" and then to what you ask.</string>
  <key>JarvisRoot</key><string>$(pwd)</string>
  <key>JarvisUV</key><string>$(command -v uv)</string>"
build_app "Jarvis Controls" com.jarvis.controls jarvisd/menubar.swift "
  <key>JarvisLogs</key><string>$(pwd)/logs</string>"

cat > ~/Library/LaunchAgents/com.jarvis.jarvisd.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- jarvisd at login, restarted if it dies. Written by launchd/install.sh  Logs: ~/Library/Logs/jarvisd.log -->
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jarvis.jarvisd</string>
  <key>ProgramArguments</key>
  <array>
    <!-- runs \`uv run python -m jarvisd\`; see jarvisd/launcher.swift for why it goes through an app -->
    <string>$HOME/Applications/Jarvis.app/Contents/MacOS/Jarvis</string>
  </array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/jarvisd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/jarvisd.log</string>
</dict>
</plist>
PLIST

cat > ~/Library/LaunchAgents/com.jarvis.menubar.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Jarvis Controls in the menu bar: on/off and costs. Written by launchd/install.sh -->
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jarvis.menubar</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/Applications/Jarvis Controls.app/Contents/MacOS/Jarvis Controls</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- restart it if it crashes, but not if Yash quits it from its menu -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST

if launchctl print-disabled "$DOMAIN" | grep -Eq '"com.jarvis.jarvisd" => (disabled|true)'; then
  echo "Jarvis is turned off (menu bar), so jarvisd stays stopped; turn it on from Jarvis Controls"
else
  launchctl bootstrap "$DOMAIN" ~/Library/LaunchAgents/com.jarvis.jarvisd.plist
fi
launchctl bootstrap "$DOMAIN" ~/Library/LaunchAgents/com.jarvis.menubar.plist
echo "jarvisd and Jarvis Controls installed; logs: tail -f ~/Library/Logs/jarvisd.log"
