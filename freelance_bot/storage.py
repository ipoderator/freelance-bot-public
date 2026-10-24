"""SQLite-хранилище: дедупликация проектов, статусы, история откликов, флаги, журнал.

Всё время в базе — UTC ISO-8601 (одинаковый формат, поэтому строки сравнимы).
«Сегодня» для дневных лимитов считается по часовому поясу `tz` (по умолчанию МСК).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from freelance_bot.models import Mode, Project, ScoreResult

STATUSES: tuple[str, ...] = (
    "new",
    "filtered_out",
    "queued",  # подошёл, ждёт дайджеста
    "notified",
    "drafted",
    "applied",
    "skipped",
    "sent",
    "failed",
)
STATS_KEYS: tuple[str, ...] = (
    "collected",
    "filtered_out",
    "notified",
    "drafted",
    "applied",
    "skipped",
    "sent",
)

# Каждая миграция — отдельный скрипт; номер версии = индекс + 1 (PRAGMA user_version).
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE projects (
        platform        TEXT NOT NULL,
        external_id     TEXT NOT NULL,
        url             TEXT NOT NULL,
        title           TEXT NOT NULL,
        description     TEXT NOT NULL,
        published_at    TEXT NOT NULL,
        budget_min      INTEGER,
        budget_max      INTEGER,
        offers_count    INTEGER,
        category        TEXT,
        deadline_days   INTEGER,
        extra           TEXT NOT NULL DEFAULT '{}',
        status          TEXT NOT NULL DEFAULT 'new',
        status_note     TEXT,
        score           INTEGER,
        score_passed    INTEGER,
        score_json      TEXT,
        first_seen_at   TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (platform, external_id)
    );
    CREATE TABLE responses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        platform        TEXT NOT NULL,
        external_id     TEXT NOT NULL,
        mode            TEXT NOT NULL,
        text            TEXT NOT NULL,
        generator       TEXT NOT NULL,
        sent            INTEGER NOT NULL,
        reason          TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE INDEX responses_created ON responses (created_at);
    CREATE TABLE flags (
        key             TEXT PRIMARY KEY,
        value           TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE TABLE events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        platform        TEXT NOT NULL,
        external_id     TEXT NOT NULL,
        kind            TEXT NOT NULL,
        note            TEXT
    );
    CREATE INDEX events_ts_kind ON events (ts, kind);
    """,
)


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("ожидается datetime с часовым поясом")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _default_clock() -> datetime:
    return datetime.now(UTC)


class Storage:
    def __init__(
        self,
        path: Path | str,
        *,
        tz: str = "Europe/Moscow",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tz = ZoneInfo(tz)
        self._clock = clock or _default_clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ── схема ────────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                self._conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def _now(self) -> str:
        return _utc_iso(self._clock())

    def _today_start(self) -> str:
        local_day = self._clock().astimezone(self._tz).date()
        return _utc_iso(datetime.combine(local_day, time.min, tzinfo=self._tz))

    def _event(self, platform: str, external_id: str, kind: str, note: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, platform, external_id, kind, note) VALUES (?, ?, ?, ?, ?)",
            (self._now(), platform, external_id, kind, note),
        )

    # ── проекты ──────────────────────────────────────────────────────────────

    def upsert_project(self, p: Project) -> bool:
        """Сохраняет проект. True — если (platform, external_id) встретился впервые."""
        now = self._now()
        data = (
            p.url,
            p.title,
            p.description,
            _utc_iso(p.published_at),
            p.budget_min,
            p.budget_max,
            p.offers_count,
            p.category,
            p.deadline_days,
            json.dumps(p.extra, ensure_ascii=False, default=str),
        )
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO projects (url, title, description, published_at, budget_min,
                    budget_max, offers_count, category, deadline_days, extra,
                    platform, external_id, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, external_id) DO NOTHING
                """,
                (*data, p.platform, p.external_id, now, now),
            )
            if cur.rowcount == 1:
                self._event(p.platform, p.external_id, "collected")
                return True
            self._conn.execute(
                """
                UPDATE projects SET url = ?, title = ?, description = ?, published_at = ?,
                    budget_min = ?, budget_max = ?, offers_count = ?, category = ?,
                    deadline_days = ?, extra = ?, updated_at = ?
                WHERE platform = ? AND external_id = ?
                """,
                (*data, now, p.platform, p.external_id),
            )
            return False

    def get_project(self, platform: str, external_id: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        return None if row is None else self._project(row)

    def recent_projects(self, since: datetime, exclude: tuple[str, ...] = ()) -> list[Project]:
        """Проекты, опубликованные с `since`, кроме статусов `exclude`; свежие первыми."""
        marks = ", ".join("?" * len(exclude))
        rows = self._conn.execute(
            f"SELECT * FROM projects WHERE published_at >= ? AND status NOT IN ({marks}) "
            "ORDER BY published_at DESC",
            (_utc_iso(since), *exclude),
        ).fetchall()
        return [self._project(row) for row in rows]

    def projects_with_status(self, status: str, since: datetime) -> list[Project]:
        """Проекты в статусе `status`, опубликованные с `since`; свежие первыми."""
        rows = self._conn.execute(
            "SELECT * FROM projects WHERE status = ? AND published_at >= ? "
            "ORDER BY published_at DESC",
            (status, _utc_iso(since)),
        ).fetchall()
        return [self._project(row) for row in rows]

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(
            platform=row["platform"],
            external_id=row["external_id"],
            url=row["url"],
            title=row["title"],
            description=row["description"],
            published_at=datetime.fromisoformat(row["published_at"]),
            budget_min=row["budget_min"],
            budget_max=row["budget_max"],
            offers_count=row["offers_count"],
            category=row["category"],
            deadline_days=row["deadline_days"],
            extra=json.loads(row["extra"]),
        )

    def set_status(
        self, platform: str, external_id: str, status: str, note: str | None = None
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"неизвестный статус {status!r}; допустимо: {', '.join(STATUSES)}")
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE projects SET status = ?, status_note = ?, updated_at = ?
                WHERE platform = ? AND external_id = ?
                """,
                (status, note, self._now(), platform, external_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"проект {platform}:{external_id} не найден")
            self._event(platform, external_id, status, note)

    def get_status(self, platform: str, external_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM projects WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        return row["status"] if row else None

    def save_score(self, platform: str, external_id: str, r: ScoreResult) -> None:
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE projects SET score = ?, score_passed = ?, score_json = ?, updated_at = ?
                WHERE platform = ? AND external_id = ?
                """,
                (
                    r.score,
                    int(r.passed),
                    json.dumps(asdict(r), ensure_ascii=False),
                    self._now(),
                    platform,
                    external_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"проект {platform}:{external_id} не найден")

    def get_score(self, platform: str, external_id: str) -> ScoreResult | None:
        row = self._conn.execute(
            "SELECT score_json FROM projects WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        if row is None or row["score_json"] is None:
            return None
        return ScoreResult(**json.loads(row["score_json"]))

    # ── отклики ──────────────────────────────────────────────────────────────

    def add_response(
        self,
        platform: str,
        external_id: str,
        mode: Mode,
        text: str,
        generator: str,
        sent: bool,
        reason: str | None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO responses (platform, external_id, mode, text, generator, sent,
                    reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, external_id, mode, text, generator, int(sent), reason, self._now()),
            )
            return int(cur.lastrowid)

    def latest_response(self, platform: str, external_id: str) -> tuple[str, str] | None:
        """Последний черновик проекта: (текст, генератор)."""
        row = self._conn.execute(
            """
            SELECT text, generator FROM responses
            WHERE platform = ? AND external_id = ? ORDER BY id DESC LIMIT 1
            """,
            (platform, external_id),
        ).fetchone()
        return None if row is None else (row["text"], row["generator"])

    def recent_drafts(self, limit: int = 20) -> list[str]:
        rows = self._conn.execute(
            "SELECT text FROM responses ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["text"] for r in rows]

    def count_auto_sent_today(self, platform: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM responses
            WHERE platform = ? AND mode = 'auto' AND sent = 1 AND created_at >= ?
            """,
            (platform, self._today_start()),
        ).fetchone()
        return int(row[0])

    def count_kwork_connects_today(self) -> int:
        """Отправленные сегодня отклики на Kwork: каждый расходует коннект."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM responses
            WHERE platform = 'kwork' AND sent = 1 AND created_at >= ?
            """,
            (self._today_start(),),
        ).fetchone()
        return int(row[0])

    # ── флаги ────────────────────────────────────────────────────────────────

    def get_flag(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM flags WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_flag(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO flags (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, self._now()),
            )

    # ── статистика ───────────────────────────────────────────────────────────

    def stats(self, since: datetime) -> dict[str, int]:
        """Сколько разных проектов прошли через каждое событие начиная с `since`."""
        rows = self._conn.execute(
            """
            SELECT kind, COUNT(DISTINCT platform || ':' || external_id) AS n
            FROM events WHERE ts >= ? GROUP BY kind
            """,
            (_utc_iso(since),),
        ).fetchall()
        counts = {r["kind"]: int(r["n"]) for r in rows}
        return {key: counts.get(key, 0) for key in STATS_KEYS}
