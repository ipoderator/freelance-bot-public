"""SQLite-хранилище: дедупликация проектов, статусы, история откликов, флаги, журнал.

Всё время в базе — UTC ISO-8601 (одинаковый формат, поэтому строки сравнимы).
«Сегодня» для дневных лимитов считается по часовому поясу `tz` (по умолчанию МСК).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from freelance_bot.models import Lesson, Mode, Project, ScoreResult, User

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
    # Публичный бот (ADR 0011): анкета пользователя. Файл резюме не хранится — только
    # выжимка в data; /forget удаляет строку целиком.
    """
    CREATE TABLE users (
        tg_id           INTEGER PRIMARY KEY,
        step            TEXT NOT NULL,
        data            TEXT NOT NULL DEFAULT '{}',
        consent_at      TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        completed_at    TEXT
    );
    """,
    # Самообучение (ADR 0013): правила из отметок «👎 Не подходит» и «✅ Откликнулся».
    # tg_id — чьи отметки (владелец или пользователь анкеты); /forget удаляет и их.
    """
    CREATE TABLE lessons (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id           INTEGER NOT NULL,
        kind            TEXT NOT NULL
            CHECK (kind IN ('topic', 'level', 'office', 'pay', 'company')),
        value           TEXT NOT NULL,
        weight          INTEGER NOT NULL,
        hits            INTEGER NOT NULL DEFAULT 1 CHECK (hits > 0),
        example         TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE (tg_id, kind, value)
    );
    """,
    # Самообучение: «🛠 не мои навыки» (skill) и стаж из описания при «🎓 не мой уровень»
    # (experience). CHECK в SQLite не меняется — таблица пересобирается, правила переносятся.
    """
    CREATE TABLE lessons_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id           INTEGER NOT NULL,
        kind            TEXT NOT NULL
            CHECK (kind IN ('topic', 'level', 'office', 'pay', 'company', 'skill', 'experience')),
        value           TEXT NOT NULL,
        weight          INTEGER NOT NULL,
        hits            INTEGER NOT NULL DEFAULT 1 CHECK (hits > 0),
        example         TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE (tg_id, kind, value)
    );
    INSERT INTO lessons_new
        (id, tg_id, kind, value, weight, hits, example, created_at, updated_at)
        SELECT id, tg_id, kind, value, weight, hits, example, created_at, updated_at
        FROM lessons;
    DROP TABLE lessons;
    ALTER TABLE lessons_new RENAME TO lessons;
    """,
    # Правила — для резюме (ADR 0018): scope — профессия анкеты, '' — profile.toml владельца.
    # Сменил резюме — правила прошлого не действуют, вернулся к нему — снова работают.
    # Правила, записанные после прохождения анкеты, переносятся в её профессию.
    """
    CREATE TABLE lessons_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id           INTEGER NOT NULL,
        scope           TEXT NOT NULL DEFAULT '',
        kind            TEXT NOT NULL
            CHECK (kind IN ('topic', 'level', 'office', 'pay', 'company', 'skill', 'experience')),
        value           TEXT NOT NULL,
        weight          INTEGER NOT NULL,
        hits            INTEGER NOT NULL DEFAULT 1 CHECK (hits > 0),
        example         TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE (tg_id, scope, kind, value)
    );
    INSERT INTO lessons_new
        (id, tg_id, scope, kind, value, weight, hits, example, created_at, updated_at)
        SELECT l.id, l.tg_id,
            COALESCE((SELECT trim(json_extract(u.data, '$.profile.profession')) FROM users u
                      WHERE u.tg_id = l.tg_id AND u.completed_at IS NOT NULL
                        AND l.created_at >= u.completed_at
                        AND json_array_length(COALESCE(json_extract(u.data, '$.directions'),
                                                       '[]')) = 0), ''),
            l.kind, l.value, l.weight, l.hits, l.example, l.created_at, l.updated_at
        FROM lessons l;
    DROP TABLE lessons;
    ALTER TABLE lessons_new RENAME TO lessons;
    """,
    # Отметки пользователя анкеты под вакансиями своего списка (ADR 0018): показано,
    # откликнулся, не подходит. Живой поиск в projects не пишется — там сбор владельца, и
    # вакансия, записанная туда раньше него, не пришла бы ему как новая; поэтому вакансия
    # (JSON) — здесь, для кнопок и обучения. /forget удаляет.
    """
    CREATE TABLE user_marks (
        tg_id           INTEGER NOT NULL,
        platform        TEXT NOT NULL,
        external_id     TEXT NOT NULL,
        mark            TEXT NOT NULL CHECK (mark IN ('shown', 'applied', 'skipped')),
        project         TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (tg_id, platform, external_id)
    );
    """,
)
MARKS: tuple[str, ...] = ("shown", "applied", "skipped")
MARKS_KEEP = timedelta(days=30)  # «показано» старше — удаляется: вакансии столько не живут


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

    # ── пользователи публичного бота ─────────────────────────────────────────

    def get_user(self, tg_id: int) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        return None if row is None else self._user(row)

    def users(self, completed: bool = True) -> list[User]:
        """Пользователи; completed — только прошедшие анкету."""
        where = "WHERE completed_at IS NOT NULL" if completed else ""
        rows = self._conn.execute(f"SELECT * FROM users {where} ORDER BY tg_id").fetchall()
        return [self._user(row) for row in rows]

    def save_user(
        self,
        tg_id: int,
        step: str,
        data: dict,
        *,
        consent: bool = False,
        completed: bool | None = None,
    ) -> User:
        """Создаёт или обновляет анкету. consent ставит время согласия один раз;
        completed: True — анкета пройдена сейчас, False — снова заполняется (рассылка по
        ней не идёт), None — не менять."""
        now = self._now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO users (tg_id, step, data, consent_at, created_at, updated_at,
                    completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tg_id) DO UPDATE SET step = excluded.step, data = excluded.data,
                    consent_at = COALESCE(users.consent_at, excluded.consent_at),
                    updated_at = excluded.updated_at,
                    completed_at = CASE WHEN ? THEN excluded.completed_at
                        ELSE users.completed_at END
                """,
                (
                    tg_id, step, json.dumps(data, ensure_ascii=False),
                    now if consent else None, now, now, now if completed else None,
                    completed is not None,
                ),  # fmt: skip
            )
        user = self.get_user(tg_id)
        assert user is not None
        return user

    def delete_user(self, tg_id: int) -> bool:
        with self._conn:
            self._conn.execute("DELETE FROM lessons WHERE tg_id = ?", (tg_id,))
            self._conn.execute("DELETE FROM user_marks WHERE tg_id = ?", (tg_id,))
            return self._conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,)).rowcount > 0

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        def when(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return User(
            tg_id=int(row["tg_id"]),
            step=row["step"],
            data=json.loads(row["data"]),
            consent_at=when(row["consent_at"]),
            completed_at=when(row["completed_at"]),
        )

    # ── самообучение (ADR 0013) ──────────────────────────────────────────────

    def lessons(self, tg_id: int, scope: str = "") -> list[Lesson]:
        """Правила человека для резюме `scope` (профессия анкеты; '' — profile.toml)."""
        rows = self._conn.execute(
            "SELECT * FROM lessons WHERE tg_id = ? AND scope = ? ORDER BY kind, updated_at DESC",
            (tg_id, scope),
        ).fetchall()
        return [
            Lesson(
                id=int(r["id"]), kind=r["kind"], value=r["value"], weight=int(r["weight"]),
                hits=int(r["hits"]), example=r["example"],
            )  # fmt: skip
            for r in rows
        ]

    def lesson_scopes(self, tg_id: int) -> dict[str, int]:
        """Сколько правил у каждого резюме человека."""
        rows = self._conn.execute(
            "SELECT scope, COUNT(*) AS n FROM lessons WHERE tg_id = ? GROUP BY scope", (tg_id,)
        ).fetchall()
        return {r["scope"]: int(r["n"]) for r in rows}

    def save_lesson(
        self, tg_id: int, kind: str, value: str, weight: int, example: str, scope: str = ""
    ) -> None:
        """Новое правило или то же ещё раз: вес заменяется, счётчик отметок растёт."""
        now = self._now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO lessons (tg_id, scope, kind, value, weight, example, created_at,
                    updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tg_id, scope, kind, value) DO UPDATE SET weight = excluded.weight,
                    hits = lessons.hits + 1, example = excluded.example,
                    updated_at = excluded.updated_at
                """,
                (tg_id, scope, kind, value, weight, example, now, now),
            )

    def delete_lesson(self, tg_id: int, lesson_id: int) -> bool:
        with self._conn:
            return (
                self._conn.execute(
                    "DELETE FROM lessons WHERE tg_id = ? AND id = ?", (tg_id, lesson_id)
                ).rowcount
                > 0
            )

    def clear_lessons(self, tg_id: int, scope: str = "") -> int:
        with self._conn:
            return self._conn.execute(
                "DELETE FROM lessons WHERE tg_id = ? AND scope = ?", (tg_id, scope)
            ).rowcount

    # ── отметки пользователей анкеты (ADR 0018) ──────────────────────────────

    def mark_shown(self, tg_id: int, projects: list[Project]) -> None:
        """Вакансии списка человека — для его кнопок; отметки «откликнулся» и «не подходит»
        не затираются. Старые «показано» удаляются."""
        now = self._now()
        with self._conn:
            for p in projects:
                self._conn.execute(
                    """
                    INSERT INTO user_marks (tg_id, platform, external_id, mark, project,
                        updated_at)
                    VALUES (?, ?, ?, 'shown', ?, ?)
                    ON CONFLICT (tg_id, platform, external_id) DO UPDATE SET
                        project = excluded.project, updated_at = excluded.updated_at
                    """,
                    (tg_id, p.platform, p.external_id, _project_json(p), now),
                )
            self._conn.execute(
                "DELETE FROM user_marks WHERE mark = 'shown' AND updated_at < ?",
                (_utc_iso(self._clock() - MARKS_KEEP),),
            )

    def set_mark(self, tg_id: int, platform: str, external_id: str, mark: str) -> bool:
        """Отметка вакансии из списка человека; её нет в его списке — False."""
        if mark not in MARKS:
            raise ValueError(f"неизвестная отметка {mark!r}; допустимо: {', '.join(MARKS)}")
        with self._conn:
            return (
                self._conn.execute(
                    "UPDATE user_marks SET mark = ?, updated_at = ? "
                    "WHERE tg_id = ? AND platform = ? AND external_id = ?",
                    (mark, self._now(), tg_id, platform, external_id),
                ).rowcount
                > 0
            )

    def marked_project(self, tg_id: int, platform: str, external_id: str) -> Project | None:
        row = self._conn.execute(
            "SELECT project FROM user_marks WHERE tg_id = ? AND platform = ? AND external_id = ?",
            (tg_id, platform, external_id),
        ).fetchone()
        return None if row is None else _project_from_json(row["project"])

    def closed_marks(self, tg_id: int) -> set[tuple[str, str]]:
        """Вакансии, на которые человек откликнулся или отметил «не подходит»."""
        rows = self._conn.execute(
            "SELECT platform, external_id FROM user_marks WHERE tg_id = ? AND mark != 'shown'",
            (tg_id,),
        ).fetchall()
        return {(r["platform"], r["external_id"]) for r in rows}

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


def _project_json(p: Project) -> str:
    raw = asdict(p)
    raw["published_at"] = _utc_iso(p.published_at)
    return json.dumps(raw, ensure_ascii=False, default=str)


def _project_from_json(text: str) -> Project:
    raw = json.loads(text)
    raw["published_at"] = datetime.fromisoformat(raw["published_at"])
    return Project(**raw)
