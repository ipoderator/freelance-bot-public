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
