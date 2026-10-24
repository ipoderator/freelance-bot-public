#!/bin/bash
# Снимает LaunchAgent бота и удаляет его plist. Данные и логи не трогает.
set -euo pipefail

LABEL="local.freelance-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "LaunchAgent $LABEL снят."
