"""Цикл сбора: фейковые источники, нотификатор и отправщик; сети нет."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_bot.config import load_settings
from freelance_bot.http import BlockedError, TemporarySourceError
from freelance_bot.models import Draft
from freelance_bot.pipeline import ConsoleNotifier, Pipeline
from freelance_bot.scoring import score_project
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)

STRONG = dict(
    title="Telegram-бот на Python (FastAPI) с интеграцией OpenAI",
    description="Нужен telegram бот, backend на Python, FastAPI, PostgreSQL, интеграция с OpenAI.",
    offers_count=1,
    budget_max=60000,
    extra={"for_all": True},  # «для всех»: отклик бесплатный (docs/platforms.md, §1)
)
PAID = {**STRONG, "extra": {}}  # FL без «для всех» — отклик платный


class FakeSource:
    def __init__(self, platform, projects=None, errors=None):
        self.platform = platform
        self.projects = list(projects or [])
        self.errors = list(errors or [])  # исключения по очереди; пусто — успех
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return list(self.projects)


class FakeNotifier:
    def __init__(self):
        self.cards: list[tuple[str, list]] = []
        self.texts: list[str] = []

    async def send_card(self, text, keyboard):
        self.cards.append((text, keyboard))

    async def send_text(self, text):
        self.texts.append(text)


class FakeDrafter:
    def __init__(self, text="Здравствуйте! Сделаю за 5 000 ₽ и 7 дней."):
        self.text = text
        self.variants: list[int] = []

    async def generate(self, p, r, variant=0):
        self.variants.append(variant)
        return Draft(
            text=f"{self.text} v{variant}",
            generator="builtin",
            price_placeholder="[ЦЕНА]" in self.text,
            deadline_placeholder="[СРОК]" in self.text,
        )


class FakeSender:
    platform = "fl"
    official_confirmed = True
    response_cost_rub = 0

    def __init__(self):
        self.sent = []

    async def send(self, p, text, price_rub, days):
        self.sent.append((p.external_id, price_rub, days))


@pytest.fixture
def settings():
    # карточки сразу; дайджест проверяют его тесты (digest_enabled=True)
    loaded = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    return replace(loaded, digest_enabled=False)


@pytest.fixture
def storage():
    s = Storage(":memory:", clock=lambda: NOW)
    yield s
    s.close()


def make_pipeline(settings, storage, sources, drafter=None, notifier=None, **kw):
    notifier = notifier or FakeNotifier()
    pipeline = Pipeline(
        settings, storage, sources, drafter or FakeDrafter(), notifier, clock=lambda: NOW, **kw
    )
    return pipeline, notifier


async def test_new_strong_project_is_notified_with_draft_and_recorded(
    settings, storage, make_project
):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])])

    report = await pipeline.run_once()

    assert (report.fetched, report.new, report.passed, report.notified) == (1, 1, 1, 1)
    assert len(notifier.cards) == 1
    assert "Telegram-бот на Python" in notifier.cards[0][0]
    assert [btn[1] for row in notifier.cards[0][1] for btn in row] == [
        "applied:fl:10", "skip:fl:10", "offer:fl:10", "redraft:fl:10",
    ]
    assert len(notifier.texts) == 1 and "Черновик отклика" in notifier.texts[0]
    assert "📝 Нужен telegram бот, backend на Python" in notifier.cards[0][0]
    assert storage.get_flag("summary:fl:10")  # суть закэширована для /last
    assert storage.get_status("fl", "10") == "drafted"
    assert storage.get_score("fl", "10").passed is True
    assert len(storage.recent_drafts()) == 1


async def test_digest_mode_queues_new_projects_instead_of_cards(
    settings, storage, make_project
):
    settings = replace(settings, digest_enabled=True)
    older = make_project(external_id="10", published_at=NOW - timedelta(hours=2), **STRONG)
    fresh = make_project(external_id="11", published_at=NOW - timedelta(hours=1), **STRONG)
    weak = make_project(
        external_id="12", published_at=NOW - timedelta(minutes=5),
        title="Логотип", description="Нарисовать логотип.",
    )
    drafter = FakeDrafter()
    pipeline, notifier = make_pipeline(
        settings, storage, [FakeSource("fl", [older, fresh, weak])], drafter=drafter
    )

    report = await pipeline.run_once()

    assert (report.passed, report.notified, report.queued) == (2, 0, 2)
    assert notifier.cards == [] and notifier.texts == [] and drafter.variants == []
    assert storage.get_status("fl", "10") == storage.get_status("fl", "11") == "queued"
    items = pipeline.digest_items()
    assert [p.external_id for p, _ in items] == ["11", "10"]  # свежие первыми

    pipeline.mark_notified([p for p, _ in items])
    assert pipeline.digest_items() == []
    assert storage.get_status("fl", "11") == "notified"
    await pipeline.run_once()  # та же лента — в дайджест повторно не попадают
    assert pipeline.digest_items() == []


async def test_second_run_over_same_feed_sends_nothing(settings, storage, make_project):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])])
    await pipeline.run_once()
    cards, texts = len(notifier.cards), len(notifier.texts)

    report = await pipeline.run_once()

    assert report.new == 0 and report.notified == 0
    assert (len(notifier.cards), len(notifier.texts)) == (cards, texts)


async def test_weak_project_is_filtered_out_silently(settings, storage, make_project):
    p = make_project(
        external_id="11", title="Нарисовать логотип", description="Дизайн логотипа в Figma",
        published_at=NOW - timedelta(hours=1),
    )
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])])

    report = await pipeline.run_once()

    assert report.new == 1 and report.passed == 0 and report.notified == 0
    assert storage.get_status("fl", "11") == "filtered_out"
    assert notifier.cards == [] and notifier.texts == []


async def test_first_run_marks_old_projects_without_notifying(settings, storage, make_project):
    old = make_project(external_id="1", published_at=NOW - timedelta(hours=30), **STRONG)
    fresh = make_project(external_id="2", published_at=NOW - timedelta(hours=1), **STRONG)
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [old, fresh])])

    await pipeline.run_once()

    assert storage.get_status("fl", "1") == "filtered_out"
    assert storage.get_status("fl", "2") == "drafted"
    assert len(notifier.cards) == 1
    assert storage.get_flag("bootstrapped:fl") == "1"


async def test_paused_does_nothing(settings, storage, make_project):
    storage.set_flag("paused", "1")
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    source = FakeSource("fl", [p])
    pipeline, notifier = make_pipeline(settings, storage, [source])

    report = await pipeline.run_once()

    assert source.calls == 0
    assert report.fetched == 0 and notifier.cards == []
    assert storage.get_status("fl", "10") is None


async def test_three_failures_in_a_row_send_one_service_message(settings, storage):
    errors = [TemporarySourceError("fl.ru: HTTP 500") for _ in range(4)]
    source = FakeSource("fl", errors=errors)
    pipeline, notifier = make_pipeline(settings, storage, [source])

    r1 = await pipeline.run_once()
    await pipeline.run_once()
    assert notifier.texts == []
    await pipeline.run_once()
    assert len(notifier.texts) == 1 and "FL.ru" in notifier.texts[0]
    await pipeline.run_once()
    assert len(notifier.texts) == 1  # без спама

    assert r1.errors and "HTTP 500" in r1.errors[0]
    assert "HTTP 500" in storage.get_flag("last_error:fl")


async def test_success_resets_failure_streak(settings, storage, make_project):
    source = FakeSource("fl", errors=[TemporarySourceError("x")] * 2)
    pipeline, notifier = make_pipeline(settings, storage, [source])
    await pipeline.run_once()
    await pipeline.run_once()
    await pipeline.run_once()  # успех
    source.errors = [TemporarySourceError("x")] * 2
    await pipeline.run_once()
    await pipeline.run_once()

    assert notifier.texts == []  # 2+2 с успехом между — не 3 подряд
    assert storage.get_flag("last_ok:fl") is not None


async def test_block_sends_message_at_once_and_pauses_source(settings, storage):
    source = FakeSource("fl", errors=[BlockedError("fl.ru: CAPTCHA")])
    pipeline, notifier = make_pipeline(settings, storage, [source])

    await pipeline.run_once()
    assert len(notifier.texts) == 1 and "блокиров" in notifier.texts[0].lower()
    until = datetime.fromisoformat(storage.get_flag("blocked_until:fl"))
    assert until > NOW

    await pipeline.run_once()
    assert source.calls == 1  # пока действует блокировка, источник не опрашивается
    assert len(notifier.texts) == 1


async def test_block_pause_lasts_one_hour(settings, storage):
    # История 8: «источник ставится на паузу (1 час)».
    source = FakeSource("fl", errors=[BlockedError("fl.ru: CAPTCHA")])
    now = [NOW]
    notifier = FakeNotifier()
    pipeline = Pipeline(settings, storage, [source], FakeDrafter(), notifier, clock=lambda: now[0])

    await pipeline.run_once()
    assert datetime.fromisoformat(storage.get_flag("blocked_until:fl")) == NOW + timedelta(hours=1)
    assert "1 ч" in notifier.texts[0]

    now[0] = NOW + timedelta(minutes=59)
    await pipeline.run_once()
    assert source.calls == 1

    now[0] = NOW + timedelta(hours=1, minutes=1)
    await pipeline.run_once()
    assert source.calls == 2  # через час источник снова опрашивается


def auto_settings(settings):
    fl = replace(settings.sources["fl"], mode="auto", daily_auto_limit=5)
    return replace(settings, sources={**settings.sources, "fl": fl}, offer_price_rub=5000,
                   offer_days=7)


async def test_dry_run_never_calls_sender_even_if_all_conditions_hold(
    settings, storage, make_project
):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    sender = FakeSender()
    pipeline, notifier = make_pipeline(
        auto_settings(settings), storage, [FakeSource("fl", [p])],
        senders={"fl": sender}, dry_run=True,
    )

    await pipeline.run_once()

    assert sender.sent == []
    assert "auto → draft" in notifier.cards[0][0] and "dry-run" in notifier.cards[0][0]
    assert storage.get_status("fl", "10") == "drafted"


async def test_auto_sends_only_when_every_condition_holds(settings, storage, make_project):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    sender = FakeSender()
    pipeline, notifier = make_pipeline(
        auto_settings(settings), storage, [FakeSource("fl", [p])],
        senders={"fl": sender}, dry_run=False,
    )

    await pipeline.run_once()

    assert sender.sent == [("10", 5000, 7)]
    assert storage.get_status("fl", "10") == "sent"
    assert storage.count_auto_sent_today("fl") == 1


async def test_platform_argument_limits_cycle(settings, storage, make_project):
    fl = FakeSource("fl", [])
    kw = FakeSource("kwork", [])
    pipeline, _ = make_pipeline(settings, storage, [fl, kw])

    await pipeline.run_once("kwork")

    assert (fl.calls, kw.calls) == (0, 1)


async def test_redraft_makes_new_variant_and_records_it(settings, storage, make_project):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    drafter = FakeDrafter()
    pipeline, _ = make_pipeline(settings, storage, [FakeSource("fl", [p])], drafter=drafter)
    await pipeline.run_once()

    redrafted = await pipeline.redraft("fl", "10")

    assert redrafted is not None and redrafted.project.external_id == "10"
    assert redrafted.draft.text.endswith("v1") and redrafted.variant == 1
    guess = pipeline.estimate(p)  # поля формы — как у выбранного заказа: оценка бота
    assert (redrafted.price_rub, redrafted.days, redrafted.own) == (
        guess.price_rub, guess.days, False
    )
    assert (await pipeline.redraft("fl", "10")).variant == 2
    assert drafter.variants == [0, 1, 2]
    assert len(storage.recent_drafts()) == 3
    assert await pipeline.redraft("fl", "no-such") is None


async def test_console_notifier_prints_plain_text(capsys):
    notifier = ConsoleNotifier()
    await notifier.send_card("<b>Бот &amp; API</b>\n<a href=\"https://x.ru\">Открыть</a>", [])
    await notifier.send_text("Служебное")
    out = capsys.readouterr().out
    assert "Бот & API" in out and "<b>" not in out and "Служебное" in out


# ── платные отклики FL (поправка G4, R05.1) ─────────────────────────────────


def free_only(settings, value: bool):
    fl = replace(settings.sources["fl"], only_free_responses=value)
    return replace(settings, sources={**settings.sources, "fl": fl})


async def test_paid_fl_project_is_filtered_out_when_only_free(settings, storage, make_project):
    free_only = replace(settings.sources["fl"], only_free_responses=True)
    settings = replace(settings, sources={**settings.sources, "fl": free_only})
    p = make_project(external_id="20", published_at=NOW - timedelta(hours=1), **PAID)
    notes = []
    set_status = storage.set_status
    storage.set_status = lambda *a, **kw: (notes.append((a, kw)), set_status(*a, **kw))[1]
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])])

    report = await pipeline.run_once()

    assert report.notified == 0 and notifier.cards == [] and notifier.texts == []
    assert storage.get_status("fl", "20") == "filtered_out"
    assert (("fl", "20", "filtered_out", "платный отклик"), {}) in notes
    assert storage.stats(NOW - timedelta(days=1))["filtered_out"] == 1  # видно в /stats


async def test_paid_fl_project_is_sent_with_mark_when_setting_is_off(
    settings, storage, make_project
):
    p = make_project(external_id="21", published_at=NOW - timedelta(hours=1), **PAID)
    pipeline, notifier = make_pipeline(
        free_only(settings, False), storage, [FakeSource("fl", [p])]
    )

    report = await pipeline.run_once()

    assert report.notified == 1
    assert "💰 отклик платный" in notifier.cards[0][0]
    assert storage.get_status("fl", "21") == "drafted"


@pytest.mark.parametrize("only_free", [True, False])
async def test_free_fl_project_is_sent_with_free_mark(settings, storage, make_project, only_free):
    p = make_project(external_id="22", published_at=NOW - timedelta(hours=1), **STRONG)
    pipeline, notifier = make_pipeline(
        free_only(settings, only_free), storage, [FakeSource("fl", [p])]
    )

    await pipeline.run_once()

    card = notifier.cards[0][0]
    assert "🆓 бесплатный отклик (нужна верификация FL)" in card
    assert "💰" not in card


async def test_kwork_project_is_not_treated_as_paid(settings, storage, make_project):
    p = make_project(
        platform="kwork", external_id="23", published_at=NOW - timedelta(hours=1), **PAID
    )
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("kwork", [p])])

    await pipeline.run_once()

    assert len(notifier.cards) == 1
    assert "💰" not in notifier.cards[0][0] and "🆓" not in notifier.cards[0][0]


async def test_offer_fills_latest_draft_with_price_and_days(settings, storage, make_project):
    p = make_project(external_id="10", published_at=NOW - timedelta(hours=1), **STRONG)
    drafter = FakeDrafter(text="Стоимость: [ЦЕНА], срок: [СРОК].")
    pipeline, _ = make_pipeline(settings, storage, [FakeSource("fl", [p])], drafter=drafter)
    await pipeline.run_once()
    await pipeline.redraft("fl", "10")  # берётся последний вариант, а не первый

    offered = await pipeline.offer("fl", "10", 15000, 5)

    assert offered is not None
    draft = offered.draft
    assert (offered.price_rub, offered.days, offered.own) == (15000, 5, True)
    assert draft.text == "Стоимость: 15 000 ₽, срок: 5 дней. v1"
    assert storage.latest_response("fl", "10") == (draft.text, "builtin")
    assert drafter.variants == [0, 1]
    assert storage.get_status("fl", "10") == "drafted"  # «откликнулся» — только кнопкой


async def test_offer_candidates_include_old_and_bootstrap_projects(settings, storage, make_project):
    old = make_project(external_id="1", published_at=NOW - timedelta(hours=30), **STRONG)
    fresh = make_project(
        external_id="2", published_at=NOW - timedelta(hours=1),
        **{**STRONG, "title": "Telegram-бот", "description": "Нужен бот."},
    )
    paid = make_project(external_id="3", published_at=NOW - timedelta(hours=2), **PAID)
    weak = make_project(
        external_id="4", published_at=NOW - timedelta(hours=1),
        title="Логотип", description="Нарисовать логотип.", extra={"for_all": True},
    )
    too_old = make_project(external_id="5", published_at=NOW - timedelta(days=8), **STRONG)
    done = make_project(external_id="6", published_at=NOW - timedelta(hours=3), **STRONG)
    for p in (old, fresh, paid, weak, too_old, done):
        storage.upsert_project(p)
    free_only = replace(settings.sources["fl"], only_free_responses=True)
    settings = replace(settings, min_score=40, sources={**settings.sources, "fl": free_only})
    storage.set_status("fl", "1", "filtered_out", "bootstrap")
    storage.set_status("fl", "6", "applied")
    pipeline, _ = make_pipeline(settings, storage, [])

    items = pipeline.offer_candidates()

    # свежий первым, хотя балл у старого выше: свежесть важнее релевантности
    assert [p.external_id for p, _ in items] == ["2", "1"]
    assert items[0][1].score < items[1][1].score
    assert len(pipeline.offer_candidates(limit=1)) == 1
    # то же время, что у «2», но балл выше (как у «1») — при равной свежести выше релевантный
    storage.upsert_project(make_project(external_id="7", published_at=fresh.published_at, **STRONG))
    assert [p.external_id for p, _ in pipeline.offer_candidates()][:3] == ["7", "2", "1"]

    paid_ok = replace(settings.sources["fl"], only_free_responses=False)
    pipeline.settings = replace(settings, sources={**settings.sources, "fl": paid_ok})
    assert "3" in [p.external_id for p, _ in pipeline.offer_candidates()]


async def test_offer_without_draft_generates_one(settings, storage, make_project):
    storage.upsert_project(make_project(external_id="11", **STRONG))  # оценки и черновика нет
    drafter = FakeDrafter(text="Цена [ЦЕНА], срок [СРОК]")
    pipeline, _ = make_pipeline(settings, storage, [], drafter=drafter)

    offered = await pipeline.offer("fl", "11", 9000, 3)

    assert offered.draft.text == "Цена 9 000 ₽, срок 3 дня v0"
    assert drafter.variants == [0]
    assert await pipeline.offer("fl", "no-such", 9000, 3) is None


async def test_response_uses_estimate_then_own_price(settings, storage, make_project):
    p = make_project(external_id="12", **STRONG)
    storage.upsert_project(p)
    storage.save_score("fl", "12", score_project(p, settings, NOW))
    drafter = FakeDrafter(text="Цена [ЦЕНА], срок [СРОК]")
    pipeline, _ = make_pipeline(settings, storage, [], drafter=drafter)

    ready = await pipeline.response("fl", "12")  # черновика нет — создаётся и сохраняется

    guess = pipeline.estimate(p)
    assert (ready.price_rub, ready.days, ready.own) == (guess.price_rub, guess.days, False)
    assert "[ЦЕНА]" not in ready.draft.text and drafter.variants == [0]
    assert storage.latest_response("fl", "12")[0] == "Цена [ЦЕНА], срок [СРОК] v0"

    await pipeline.offer("fl", "12", 9000, 3)
    await pipeline.redraft("fl", "12")  # новый вариант с метками — цена пользователя остаётся
    ready = await pipeline.response("fl", "12")
    assert ready.own and ready.draft.text == "Цена 9 000 ₽, срок 3 дня v1"
    assert drafter.variants == [0, 1]
    assert await pipeline.response("fl", "no-such") is None


# --- оценка цены и срока (pricing.py) -------------------------------------------


async def test_card_and_draft_show_estimate_but_storage_keeps_placeholders(
    settings, storage, make_project
):
    p = make_project(external_id="20", published_at=NOW - timedelta(hours=1), **STRONG)
    drafter = FakeDrafter(text="Стоимость: [ЦЕНА], срок: [СРОК].")
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])], drafter=drafter)

    await pipeline.run_once()

    guess = pipeline.estimate(p)
    price = f"{guess.price_rub:,}".replace(",", " ") + " ₽"
    assert "🧮 Оценка:" in notifier.cards[0][0] and price in notifier.cards[0][0]
    shown = notifier.texts[0]
    assert price in shown and "[ЦЕНА]" not in shown and "[СРОК]" not in shown
    assert "оценка бота" in shown
    # в базе — черновик с метками: кнопка «Цена и срок» впишет туда цену пользователя
    assert storage.latest_response("fl", "20")[0] == "Стоимость: [ЦЕНА], срок: [СРОК]. v0"
    offered = await pipeline.offer("fl", "20", 15000, 5)
    assert offered.draft.text == "Стоимость: 15 000 ₽, срок: 5 дней. v0"


async def test_fixed_price_from_settings_is_not_replaced_by_estimate(
    settings, storage, make_project
):
    p = make_project(external_id="21", published_at=NOW - timedelta(hours=1), **STRONG)
    drafter = FakeDrafter(text="Стоимость: 7 000 ₽, срок: 2 дня.")
    pipeline, notifier = make_pipeline(settings, storage, [FakeSource("fl", [p])], drafter=drafter)

    await pipeline.run_once()

    assert "Стоимость: 7 000 ₽, срок: 2 дня." in notifier.texts[0]
    assert "оценка бота" not in notifier.texts[0]
    redrafted = await pipeline.redraft("fl", "21")
    assert redrafted.draft.text == "Стоимость: 7 000 ₽, срок: 2 дня. v1"
