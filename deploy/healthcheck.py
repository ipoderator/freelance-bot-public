"""Healthcheck контейнера: была ли успешная выборка с площадки за последние 30 минут.

HTTP-порта у бота нет, поэтому живость — по флагам в SQLite (storage.py: last_ok:<площадка>).
Пауза по /pause — не сбой. Код выхода 0 — здоров, 1 — нет.
"""

import sqlite3
import sys
from datetime import UTC, datetime, timedelta

DB = "data/bot.sqlite3"
MAX_AGE = timedelta(minutes=30)

try:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    flags = dict(conn.execute("SELECT key, value FROM flags").fetchall())
except sqlite3.Error as exc:
    print(f"нет доступа к {DB}: {exc}")
    sys.exit(1)

if flags.get("paused") == "1":
    sys.exit(0)
now = datetime.now(UTC)
fresh = [
    key
    for key, value in flags.items()
    if key.startswith("last_ok:") and value and now - datetime.fromisoformat(value) < MAX_AGE
]
print("ok: " + ", ".join(fresh) if fresh else f"нет успешного сбора за {MAX_AGE}")
sys.exit(0 if fresh else 1)
