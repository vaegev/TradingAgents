#!/bin/bash
# The daily autonomous run, as a macOS launchd job.
#
#   ./schedule.sh install    run Tue-Sat at 09:00 local time (after each US session)
#   ./schedule.sh remove     stop scheduling it
#   ./schedule.sh status     whether it is loaded, its last exit code, the log tail
#   ./schedule.sh run-now    start the job immediately, as the schedule would
#
# A Mac asleep at 09:00 runs the job when it wakes. The run itself keeps the Mac
# awake (caffeinate -i) until it finishes. HOUR=8 MINUTE=30 ./schedule.sh install
# picks another time.
set -euo pipefail

LABEL="com.tradingagents.watchlist"
REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.tradingagents/logs/watchlist/daily.log"
DOMAIN="gui/$(id -u)"
HOUR="${HOUR:-9}"
MINUTE="${MINUTE:-0}"

install() {
    [ -x "$REPO/.venv/bin/python" ] || { echo "no $REPO/.venv; create it first"; exit 1; }
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    # launchd weekdays: 1 = Monday ... 6 = Saturday. Tue-Sat follows the Mon-Fri sessions.
    local intervals=""
    for day in 2 3 4 5 6; do
        intervals+="<dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>"
    done
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string><string>-i</string>
        <string>$REPO/.venv/bin/python</string>
        <string>$REPO/run_watchlist.py</string>
        <string>--alpaca</string><string>--execute</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>StartCalendarInterval</key><array>$intervals</array>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
    <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    printf 'Installed: Tue-Sat at %02d:%02d local time. Log: %s\n' "$HOUR" "$MINUTE" "$LOG"
}

remove() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. Nothing will run on a schedule."
}

status() {
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        echo "Loaded: $PLIST"
        launchctl print "$DOMAIN/$LABEL" | grep -E "^\s+(state|runs|last exit code) =" || true
    else
        echo "Not installed."
    fi
    [ -f "$HOME/.tradingagents/STOP" ] && echo "Kill switch ON: ~/.tradingagents/STOP exists, no orders will be placed."
    [ -f "$LOG" ] && { echo "--- last lines of $LOG"; tail -n 25 "$LOG"; }
    return 0
}

case "${1:-}" in
    install) install ;;
    remove) remove ;;
    status) status ;;
    run-now) launchctl kickstart "$DOMAIN/$LABEL" && echo "Started. Follow it with: tail -f $LOG" ;;
    *) sed -n '2,11p' "$0"; exit 1 ;;
esac
