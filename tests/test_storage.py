from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from freelance_bot.models import ScoreResult
from freelance_bot.storage import STATUSES, Storage

MSK = ZoneInfo("Europe/Moscow")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    # 13:00 по Москве
    return Clock(datetime(2026, 10, 21, 10, 0, tzinfo=UTC))


@pytest.fixture
def storage(clock: Clock) -> Storage:
    return Storage(":memory:", clock=clock)


def test_upsert_returns_true_only_for_new_platform_and_id(storage: Storage, make_project) -> None:
    assert storage.upsert_project(make_project(platform="fl", external_id="100")) is True
    assert storage.upsert_project(make_project(platform="fl", external_id="100")) is False
    assert (
        storage.upsert_project(make_project(platform="fl", external_id="100", title="Новое имя"))
        is False
    )
    assert storage.upsert_project(make_project(platform="kwork", external_id="100")) is True
    assert storage.upsert_project(make_project(platform="fl", external_id="101")) is True


def test_new_project_has_status_new_and_status_changes(storage: Storage, make_project) -> None:
    storage.upsert_project(make_project(external_id="7"))
    assert storage.get_status("fl", "7") == "new"
    storage.set_status("fl", "7", "notified")
    storage.set_status("fl", "7", "drafted", note="вариант 1")
    assert storage.get_status("fl", "7") == "drafted"
    storage.set_status("fl", "7", "applied")
    assert storage.get_status("fl", "7") == "applied"


def test_repeated_upsert_keeps_status(storage: Storage, make_project) -> None:
    storage.upsert_project(make_project(external_id="7"))
    storage.set_status("fl", "7", "skipped")
    storage.upsert_project(make_project(external_id="7", offers_count=10))
    assert storage.get_status("fl", "7") == "skipped"


def test_statuses_list_and_validation(storage: Storage, make_project) -> None:
    assert set(STATUSES) == {
        "new",
        "filtered_out",
        "queued",
        "notified",
        "drafted",
        "applied",
        "skipped",
        "sent",
        "failed",
    }
    storage.upsert_project(make_project(external_id="7"))
    with pytest.raises(ValueError, match="bogus"):
        storage.set_status("fl", "7", "bogus")
    with pytest.raises(KeyError):
        storage.set_status("fl", "nope", "notified")
    assert storage.get_status("fl", "nope") is None


def test_get_project_roundtrip(storage: Storage, make_project) -> None:
    original = make_project(
        platform="kwork",
        external_id="555",
        deadline_days=3,
        budget_min=1000,
        offers_count=None,
        extra={"wants_count": 12, "hired_percent": 40},
    )
    storage.upsert_project(original)
    loaded = storage.get_project("kwork", "555")
    assert loaded == original
    assert loaded.published_at.utcoffset() == timedelta(0)
    assert storage.get_project("kwork", "missing") is None


def test_save_score_roundtrip(storage: Storage, make_project) -> None:
    storage.upsert_project(make_project(external_id="9"))
    result = ScoreResult(
        score=77,
        passed=True,
        directions=["telegram", "backend"],
        matched_terms=["telegram", "fastapi"],
        excluded_reason=None,
        reasons=["Telegram-бот + backend"],
        client_requirements=["в отклике укажите сроки"],
    )
    storage.save_score("fl", "9", result)
    assert storage.get_score("fl", "9") == result
    assert storage.get_score("fl", "404") is None


def test_responses_history_and_recent_drafts(storage: Storage, make_project) -> None:
    storage.upsert_project(make_project(external_id="1"))
    ids = [
        storage.add_response("fl", "1", "draft", f"черновик {i}", "builtin", False, None)
        for i in range(25)
    ]
    assert ids == sorted(ids) and len(set(ids)) == 25
    recent = storage.recent_drafts()
    assert len(recent) == 20
    assert recent[0] == "черновик 24"
    assert recent[-1] == "черновик 5"
    assert storage.recent_drafts(limit=2) == ["черновик 24", "черновик 23"]


def test_count_auto_sent_today_uses_moscow_day(
    storage: Storage, clock: Clock, make_project
) -> None:
    storage.upsert_project(make_project(platform="fl", external_id="1"))
    storage.upsert_project(make_project(platform="kwork", external_id="2"))

    # 23:30 МСК 20 октября — вчера
    clock.now = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    storage.add_response("fl", "1", "auto", "t", "builtin", True, None)
    # 00:30 МСК 21 октября — уже сегодня
    clock.now = datetime(2026, 10, 20, 21, 30, tzinfo=UTC)
    storage.add_response("fl", "1", "auto", "t", "builtin", True, None)
    clock.now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    storage.add_response("fl", "1", "auto", "t", "llm", True, None)
    storage.add_response("fl", "1", "auto", "t", "builtin", False, "dry-run")  # не отправлено
    storage.add_response("fl", "1", "draft", "t", "builtin", True, None)  # не auto
    storage.add_response("kwork", "2", "auto", "t", "builtin", True, None)  # другая площадка

    clock.now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    assert storage.count_auto_sent_today("fl") == 2
    assert storage.count_auto_sent_today("kwork") == 1


def test_count_kwork_connects_today(storage: Storage, clock: Clock, make_project) -> None:
    storage.upsert_project(make_project(platform="kwork", external_id="2"))
    storage.upsert_project(make_project(platform="fl", external_id="1"))
    assert storage.count_kwork_connects_today() == 0
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # вчера
    storage.add_response("kwork", "2", "auto", "t", "builtin", True, None)
    clock.now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    storage.add_response("kwork", "2", "auto", "t", "builtin", True, None)
    storage.add_response("kwork", "2", "auto", "t", "builtin", False, "лимит")
    storage.add_response("fl", "1", "auto", "t", "builtin", True, None)
    assert storage.count_kwork_connects_today() == 1


def test_flags(storage: Storage) -> None:
    assert storage.get_flag("paused") is None
    storage.set_flag("paused", "1")
    assert storage.get_flag("paused") == "1"
    storage.set_flag("paused", "0")
    assert storage.get_flag("paused") == "0"
    storage.set_flag("last_ok:fl", "2026-10-21T10:00:00+00:00")
    assert storage.get_flag("last_ok:fl") == "2026-10-21T10:00:00+00:00"
    assert storage.get_flag("last_ok:kwork") is None


def test_stats_since(storage: Storage, clock: Clock, make_project) -> None:
    clock.now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # до окна
    storage.upsert_project(make_project(external_id="old"))
    storage.set_status("fl", "old", "notified")

    clock.now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    for i in range(1, 6):
        assert storage.upsert_project(make_project(external_id=str(i)))
    storage.upsert_project(make_project(external_id="1"))  # повтор — не «собран» второй раз
    storage.set_status("fl", "1", "filtered_out")
    storage.set_status("fl", "2", "filtered_out")
    storage.set_status("fl", "3", "notified")
    storage.set_status("fl", "3", "drafted")
    storage.set_status("fl", "3", "drafted")  # «другой вариант» — тот же проект
    storage.set_status("fl", "3", "applied")
    storage.set_status("fl", "4", "notified")
    storage.set_status("fl", "4", "skipped")
    storage.set_status("fl", "5", "notified")
    storage.set_status("fl", "5", "sent")
    storage.set_status("fl", "old", "skipped")  # старый проект, действие в окне

    since = datetime(2026, 10, 21, 0, 0, tzinfo=MSK)
    assert storage.stats(since) == {
        "collected": 5,
        "filtered_out": 2,
        "notified": 3,
        "drafted": 1,
        "applied": 1,
        "skipped": 2,
        "sent": 1,
    }
    assert storage.stats(datetime(2026, 10, 22, tzinfo=UTC)) == dict.fromkeys(
        ("collected", "filtered_out", "notified", "drafted", "applied", "skipped", "sent"), 0
    )


def test_file_database_persists_and_has_schema_version(tmp_path: Path, make_project) -> None:
    db = tmp_path / "data" / "bot.sqlite3"
    first = Storage(db)
    first.upsert_project(make_project(external_id="1"))
    first.set_flag("paused", "1")
    first.close()

    second = Storage(db)
    assert second.upsert_project(make_project(external_id="1")) is False
    assert second.get_flag("paused") == "1"
    second.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "responses", "flags", "events"} <= tables


def test_latest_response_and_recent_projects(storage: Storage, make_project) -> None:
    for number, hour in (("1", 8), ("2", 9), ("3", 7)):
        storage.upsert_project(
            make_project(external_id=number, published_at=datetime(2026, 10, 21, hour, tzinfo=UTC))
        )
    storage.set_status("fl", "1", "notified")
    storage.set_status("fl", "2", "drafted")
    storage.set_status("fl", "3", "applied")
    assert storage.latest_response("fl", "1") is None
    storage.add_response("fl", "1", "draft", "первый", "builtin", False, None)
    storage.add_response("fl", "1", "draft", "второй", "llm", False, None)
    assert storage.latest_response("fl", "1") == ("второй", "llm")

    since = datetime(2026, 10, 21, 8, tzinfo=UTC)
    assert [p.external_id for p in storage.recent_projects(since, ("applied",))] == ["2", "1"]
    assert [p.external_id for p in storage.recent_projects(since)] == ["2", "1"]
    assert [p.external_id for p in storage.recent_projects(since - timedelta(hours=1))] == [
        "2", "1", "3",
    ]


def test_lessons_table_migrates_from_previous_version(tmp_path: Path) -> None:
    from freelance_bot.storage import MIGRATIONS

    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        for script in MIGRATIONS[:-1]:
            conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
        conn.execute(
            "INSERT INTO users (tg_id, step, data, created_at, updated_at) "
            "VALUES (7, 'done', '{}', 'x', 'x')"
        )
    storage = Storage(db)
    assert storage.get_user(7) is not None  # данные целы
    storage.save_lesson(7, "topic", "аналитик", -10, "Бизнес-аналитик")
    storage.save_lesson(7, "topic", "аналитик", -20, "Системный аналитик")
    [lesson] = storage.lessons(7)
    assert (lesson.weight, lesson.hits, lesson.example) == (-20, 2, "Системный аналитик")
    with pytest.raises(sqlite3.IntegrityError):
        storage.save_lesson(7, "nonsense", "x", 0, "")
    storage.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_lessons_are_per_user_and_forget_deletes_them(storage: Storage) -> None:
    storage.save_user(7, "done", {})
    storage.save_lesson(7, "company", "ромашка", 0, "Аналитик")
    storage.save_lesson(7, "office", "", -15, "Аналитик")
    storage.save_lesson(8, "topic", "педагог", -10, "Педагог")
    assert [x.kind for x in storage.lessons(7)] == ["company", "office"]

    first = storage.lessons(7)[0]
    assert storage.delete_lesson(8, first.id) is False  # чужое правило не удаляется
    assert storage.delete_lesson(7, first.id) is True
    assert storage.delete_user(7) is True
    assert storage.lessons(7) == []
    assert [x.value for x in storage.lessons(8)] == ["педагог"]
    assert storage.clear_lessons(8) == 1 and storage.lessons(8) == []


def test_lessons_rebuilt_for_skill_and_experience_keep_rules(tmp_path: Path) -> None:
    # миграция 4: CHECK пересобран под skill/experience, правила версии 3 перенесены
    from freelance_bot.storage import MIGRATIONS

    db = tmp_path / "v3.sqlite3"
    with sqlite3.connect(db) as conn:
        for script in MIGRATIONS[:3]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO lessons (tg_id, kind, value, weight, hits, example, created_at, "
            "updated_at) VALUES (7, 'topic', 'аналитик', -30, 2, 'Бизнес-аналитик', 'a', 'b')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lessons (tg_id, kind, value, weight, created_at, updated_at) "
                "VALUES (7, 'skill', 'java', -10, 'a', 'b')"
            )
    storage = Storage(db)
    [old] = storage.lessons(7)
    assert (old.kind, old.value, old.weight, old.hits, old.example) == (
        "topic", "аналитик", -30, 2, "Бизнес-аналитик",
    )  # fmt: skip
    storage.save_lesson(7, "skill", "java", -10, "Java-разработчик")
    storage.save_lesson(7, "experience", "", 3, "Python-разработчик")
    assert {x.kind for x in storage.lessons(7)} == {"topic", "skill", "experience"}
    with pytest.raises(sqlite3.IntegrityError):
        storage.save_lesson(7, "nonsense", "x", 0, "")
    storage.save_lesson(7, "topic", "педагог", -15, "")  # автонумерация после переноса
    assert len({x.id for x in storage.lessons(7)}) == 4
    storage.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_lessons_get_resume_scope_and_old_rules_keep_working(tmp_path: Path) -> None:
    """Миграция 5: правила, записанные после прохождения анкеты, — для её профессии; более
    ранние и у разработчика — '' (profile.toml); при смене резюме чужие не действуют."""
    import json

    from freelance_bot.storage import MIGRATIONS

    db = tmp_path / "v4.sqlite3"
    with sqlite3.connect(db) as conn:
        for script in MIGRATIONS[:4]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 4")
        anketa = {"profile": {"profession": " Продавец-консультант "}, "directions": []}
        dev = {"profile": {"profession": "Python-разработчик"}, "directions": ["backend"]}
        rows = [(7, json.dumps(anketa, ensure_ascii=False), "2026-10-20T00:00:00+00:00"),
                (8, json.dumps(dev, ensure_ascii=False), "2026-10-20T00:00:00+00:00")]
        conn.executemany(
            "INSERT INTO users (tg_id, step, data, created_at, updated_at, completed_at) "
            "VALUES (?, 'done', ?, 'a', 'b', ?)", rows,
        )  # fmt: skip
        lessons = [(7, "company", "роза хутор", "2026-10-19T00:00:00+00:00"),  # до анкеты
                   (7, "company", "пятерочка", "2026-10-21T00:00:00+00:00"),  # после
                   (8, "topic", "java", "2026-10-21T00:00:00+00:00"),  # разработчик
                   (9, "topic", "php", "2026-10-21T00:00:00+00:00")]  # без анкеты
        conn.executemany(
            "INSERT INTO lessons (tg_id, kind, value, weight, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?)", [(*x, x[3]) for x in lessons],
        )  # fmt: skip
    storage = Storage(db)
    assert [x.value for x in storage.lessons(7, "Продавец-консультант")] == ["пятерочка"]
    assert [x.value for x in storage.lessons(7)] == ["роза хутор"]
    assert [x.value for x in storage.lessons(8)] == ["java"]
    assert [x.value for x in storage.lessons(9)] == ["php"]
    assert storage.lesson_scopes(7) == {"": 1, "Продавец-консультант": 1}
    # одно правило у двух резюме — разные строки
    storage.save_lesson(7, "company", "пятерочка", 0, "Кассир", scope="")
    assert len(storage.lessons(7)) == 2 and len(storage.lessons(7, "Продавец-консультант")) == 1
    assert storage.clear_lessons(7, "Продавец-консультант") == 1 and storage.lessons(7)
    storage.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_user_marks_per_person_and_forget(storage: Storage, make_project) -> None:
    """Отметки под списком — у каждого свои; «показано» не затирает «откликнулся»; вакансия
    хранится целиком (живой поиск в projects не пишется); /forget удаляет."""
    p = make_project(platform="superjob", external_id="5", title="Повар",
                     extra={"area": "Воронеж", "company": "Кафе"})  # fmt: skip
    storage.save_user(7, "done", {"full_name": "Иванов Иван"}, consent=True, completed=True)
    storage.mark_shown(7, [p])
    storage.mark_shown(8, [p])
    assert storage.marked_project(7, "superjob", "5") == p
    assert storage.get_project("superjob", "5") is None  # сбор владельца не тронут
    assert storage.set_mark(7, "superjob", "5", "applied") is True
    assert storage.set_mark(7, "superjob", "404", "applied") is False  # не из его списка
    storage.mark_shown(7, [p])  # показали снова — отметка осталась
    assert storage.closed_marks(7) == {("superjob", "5")} and storage.closed_marks(8) == set()
    with pytest.raises(ValueError):
        storage.set_mark(7, "superjob", "5", "sent")
    storage.save_lesson(7, "company", "кафе", 0, "Повар", scope="Повар")
    assert storage.delete_user(7) is True
    assert storage.marked_project(7, "superjob", "5") is None
    assert storage.lesson_scopes(7) == {} and storage.marked_project(8, "superjob", "5") == p


def test_old_shown_marks_are_dropped(make_project) -> None:
    clock = [datetime(2026, 10, 1, tzinfo=UTC)]
    storage = Storage(":memory:", clock=lambda: clock[0])
    old, fresh = (make_project(platform="superjob", external_id=i) for i in ("1", "2"))
    storage.mark_shown(7, [old])
    clock[0] += timedelta(days=31)
    storage.mark_shown(7, [fresh])
    assert storage.marked_project(7, "superjob", "1") is None
    assert storage.marked_project(7, "superjob", "2") is not None
