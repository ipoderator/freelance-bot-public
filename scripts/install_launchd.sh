#!/bin/bash
# Ставит LaunchAgent: бот стартует при входе в систему и перезапускается при падении.
# Использование: scripts/install_launchd.sh            — записать plist и загрузить
#                scripts/install_launchd.sh --print    — только вывести plist (ничего не делает)
set -euo pipefail

LABEL="local.freelance-bot"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT/logs"

render() {
cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>freelance_bot</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/freelance-bot.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/freelance-bot.log</string>
</dict>
</plist>
PLIST
}

if [ "${1:-}" = "--print" ]; then
    render
    exit 0
fi

if [ ! -x "$PYTHON" ]; then
    echo "Не найден $PYTHON. Сначала: make install" >&2
    exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
    echo "Нет файла .env (токен бота). Сначала настройте бота — см. README." >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
render > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Готово: $LABEL загружен. Логи: $LOG_DIR/freelance-bot.log"
echo "Состояние: launchctl print gui/$(id -u)/$LABEL"
