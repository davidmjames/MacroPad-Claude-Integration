#!/usr/bin/env bash
# Installs the daemon as a launchd user agent. Does NOT touch ~/.claude/settings.json;
# run scripts/install_hooks.py for that step so you can review it separately.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-macropad.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$HERE/.venv/bin/python" ]; then
  python3 -m venv "$HERE/.venv"
fi
"$HERE/.venv/bin/pip" install -q -r "$HERE/daemon/requirements.txt"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#__LABEL__#$LABEL#g" \
    -e "s#__PYTHON__#$HERE/.venv/bin/python#g" \
    -e "s#__DAEMON__#$HERE/daemon/macropad_daemon.py#g" \
    -e "s#__HOME__#$HOME#g" \
    "$HERE/launchd/daemon.plist.template" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "daemon running under launchd as $LABEL"
echo "  log:     tail -f ~/Library/Logs/claude-macropad.log"
echo "  stop:    launchctl bootout gui/$(id -u)/$LABEL"
echo "next:      python3 $HERE/scripts/install_hooks.py"
