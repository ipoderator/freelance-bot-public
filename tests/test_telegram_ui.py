"""Форматирование карточек и сообщений бота (без сети)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from freelance_bot.config import load_settings
from freelance_bot.models import Draft, ModeDecision, ScoreResult
from freelance_bot.pricing import Estimate
from freelance_bot.telegram_ui import (
    card_keyboard,
    fmt_when,
    format_applied,
    format_card,
    format_draft,
    format_filters,
    format_offer,
    format_offer_list,
    format_offer_prompt,
    format_stats,
    format_status,
    offer_list_keyboard,
    selection_reason,
)

ROOT = Path(__file__).resolve().parent.parent


def _result(**kw) -> ScoreResult:
    values = dict(
        score=72,
        passed=True,
        directions=["telegram", "backend"],
        matched_terms=["telegram", "python"],
        excluded_reason=None,
        reasons=["Направление «telegram»: telegram", "⚠ требование: стаж от 3 лет (−15)"],
        client_requirements=[],
    )
    values.update(kw)
    return ScoreResult(**values)


def _decision(**kw) -> ModeDecision:
    values = dict(requested="draft", effective="draft", reasons=[], send=False)
    values.update(kw)
    return ModeDecision(**values)


def test_card_has_all_fields_in_moscow_time(make_project):
    p = make_project(
        title="Бот для магазина",
        published_at=datetime(2026, 10, 21, 9, 5, tzinfo=UTC),
        budget_max=30000,
        offers_count=3,
    )
    text = format_card(p, _result(), _decision(), "Europe/Moscow")
    assert "Бот для магазина" in text
    assert "FL.ru" in text
    assert "12:05" in text and "21.10.2026" in text  # 09:05 UTC = 12:05 МСК
    assert "30 000" in text
    assert "откликов: 3" in text
    assert "72" in text
    assert "Направление «telegram»" in text
    assert "стаж от 3 лет" in text
    assert p.url in text


def test_card_escapes_html_in_title_and_description_fields(make_project):
    p = make_project(title="<b>Бот</b> & <script>", url="https://x.ru/?a=1&b=2")
    text = format_card(p, _result(reasons=["термин <i>"]), _decision(), "Europe/Moscow")
    assert "<script>" not in text
    assert "&lt;b&gt;Бот&lt;/b&gt; &amp; &lt;script&gt;" in text
    assert "&lt;i&gt;" in text
    assert "a=1&amp;b=2" in text


def test_card_without_budget_and_offers_says_no_data(make_project):
    p = make_project(budget_min=None, budget_max=None, offers_count=None)
    text = format_card(p, _result(), _decision(), "Europe/Moscow")
    assert "откликов: нет данных" in text
    assert "бюджет: не указан" in text


def test_card_shows_auto_to_draft_degradation(make_project):
    d = _decision(requested="auto", effective="draft", reasons=["лимит исчерпан", "нет sender"])
    text = format_card(make_project(), _result(), d, "Europe/Moscow")
    assert "auto → draft: лимит исчерпан; нет sender" in text


def test_card_has_no_degradation_line_when_modes_match(make_project):
    text = format_card(make_project(), _result(), _decision(), "Europe/Moscow")
    assert "auto →" not in text


def test_card_marks_free_fl_response(make_project):
    p = make_project(extra={"for_all": True})
    assert "бесплатный отклик" in format_card(p, _result(), _decision(), "Europe/Moscow")
    plain = format_card(make_project(), _result(), _decision(), "Europe/Moscow")
    assert "бесплатный отклик" not in plain


def test_card_marks_fl_response_cost(make_project):
    free_project = make_project(extra={"for_all": True})
    free = format_card(free_project, _result(), _decision(), "Europe/Moscow")
    assert "🆓 бесплатный отклик (нужна верификация FL)" in free and "💰" not in free
    paid = format_card(make_project(), _result(), _decision(), "Europe/Moscow")
    assert "💰 отклик платный" in paid and "🆓" not in paid
    kwork = format_card(make_project(platform="kwork"), _result(), _decision(), "Europe/Moscow")
    assert "💰" not in kwork and "🆓" not in kwork  # на Kwork отклик — бесплатный коннект


def test_draft_message_is_escaped_and_keeps_placeholders():
    d = Draft(
        text="Цена [ЦЕНА] <b>x</b>", generator="builtin",
        price_placeholder=True, deadline_placeholder=False,
    )
    out = format_draft(d)
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert "[ЦЕНА]" in out


def test_keyboard_callbacks():
    kb = card_keyboard("fl", "123")
    flat = [btn for row in kb for btn in row]
    assert ("✅ Откликнулся", "applied:fl:123") in flat
    assert ("⏭ Пропустить", "skip:fl:123") in flat
    assert ("🔄 Другой вариант", "redraft:fl:123") in flat
    assert all(len(data.encode()) <= 64 for _, data in flat)


def test_stats_today_and_week():
    today = dict(collected=5, filtered_out=3, notified=2, drafted=2, applied=1, skipped=0, sent=0)
    week = dict(collected=40, filtered_out=30, notified=10, drafted=9, applied=4, skipped=2, sent=0)
    out = format_stats(today, week)
    assert "Сегодня" in out and "7 дней" in out
    assert "🔎 собрано: <b>5</b> / 40" in out and "✅ откликнулся: <b>1</b> / 4" in out
    assert "откликнулись на 4 из 10 присланных (40%)" in out
    assert "отправлено авто" not in out  # автоотправки нет — строка не показывается


def test_filters_lists_directions_exclusions_and_limits():
    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    out = format_filters(s)
    assert "backend" in out and "telegram" in out
    assert "min_score" in out and f"min_score: {s.min_score}" in out
    assert "design" in out
    assert "draft" in out


def test_status_paused_and_kwork_warning():
    from dataclasses import replace

    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    s = replace(s, sources={**s.sources, "kwork": replace(s.sources["kwork"], enabled=False)})
    flags = {"last_ok:fl": "2026-10-21T09:00:00+00:00", "last_error:fl": ""}
    out = format_status(s, paused=True, flags=flags)
    assert "на паузе" in out
    assert "12:00" in out  # последний успешный сбор FL в МСК
    assert "Kwork" in out
    assert "соглашени" not in out  # Kwork выключен — предупреждения нет


def test_status_warns_when_kwork_enabled():
    from dataclasses import replace

    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    kwork = replace(s.sources["kwork"], enabled=True)
    s = replace(s, sources={**s.sources, "kwork": kwork})
    out = format_status(s, paused=False, flags={"last_error:kwork": "HTTP 500"})
    assert "соглашени" in out
    assert "работает" in out
    assert "HTTP 500" in out


def test_offer_list_and_keyboard(make_project):
    assert "подходящих заказов" in format_offer_list([], "Europe/Moscow")
    items = [
        (make_project(external_id="5", title="Бот <для> CRM" + " очень" * 10), 64),
        (make_project(external_id="6", title="Парсер", extra={"for_all": True}), None),
    ]
    out = format_offer_list(items, "Europe/Moscow")
    assert "<b>1.</b> <a href=" in out and "Бот &lt;для&gt; CRM" in out
    assert "🕐 21.10.2026 12:00 МСК · FL.ru · ⭐ 64" in out
    assert out.count("💰 платный отклик") == 1 and "💵 30 000 ₽" in out
    kb = offer_list_keyboard(items)
    assert [row[0][1] for row in kb] == ["pick:fl:5", "pick:fl:6"]
    assert kb[0][0][0].endswith("…") and len(kb[0][0][0]) <= 43
    assert kb[1][0][0] == "2. Парсер"
    tail = format_offer_list(items[1:], "Europe/Moscow", start=12)  # продолжение списка
    assert tail.startswith("<b>12.</b> <a href=") and "Подходящие" not in tail
    assert offer_list_keyboard(items[1:], start=12)[0][0][0] == "12. Парсер"


def test_offer_prompt_and_ready_offer_are_escaped(make_project):
    p = make_project(title="Сайт <script>")
    assert "Сайт &lt;script&gt;" in format_offer_prompt(p)
    d = Draft("Цена 5 000 ₽ <b>", "builtin", False, False)
    out = format_offer(p, d, ["[ПРИМЕРЫ РАБОТ — впиши]"], 5000, 1)
    assert "<pre>Цена 5 000 ₽ &lt;b&gt;</pre>" in out
    assert "⚠ Перед отправкой впишите: [ПРИМЕРЫ РАБОТ — впиши]" in out
    assert "• Срок: <code>1</code> (день)" in out and "оценка бота" in out
    assert "до трёх работ" in out
    kwork = format_offer(make_project(platform="kwork"), d, [], 5000, 3, own=True)
    assert "⚠" not in kwork and "(ваши)" in kwork and "(дня)" in kwork
    assert "обнуляет коннекты" in kwork and "до трёх работ" not in kwork


def test_applied_confirmation_is_escaped(make_project):
    out = format_applied(make_project(platform="kwork", title="Бот <CRM>"))
    assert "«Бот &lt;CRM&gt;» (Kwork)" in out and out.startswith("✅")


def test_card_and_offer_texts_show_summary(make_project):
    p = make_project()
    card = format_card(p, _result(), _decision(), "Europe/Moscow", "Бот <для> CRM.")
    assert card.splitlines()[1] == "📝 Бот &lt;для&gt; CRM."
    assert "📝" not in format_card(p, _result(), _decision(), "Europe/Moscow")
    assert "📝 Суть." in format_offer_prompt(p, "Суть.")
    out = format_offer_list([(p, 50)], "Europe/Moscow", ["Суть заказа."])
    assert "\n📝 Суть заказа.\n" in out


ESTIMATE = Estimate(price_rub=12000, days=5, basis="бот; бюджет <не указан>", below_budget=False)


def test_card_shows_estimate_line(make_project):
    text = format_card(make_project(), _result(), _decision(), "Europe/Moscow", "", ESTIMATE)
    assert "🧮 Оценка: <b>12 000 ₽ · 5 дней</b> — бот; бюджет &lt;не указан&gt;" in text
    assert "🧮" not in format_card(make_project(), _result(), _decision(), "Europe/Moscow")


def test_offer_prompt_and_list_show_estimate(make_project):
    p = make_project()
    prompt = format_offer_prompt(p, "", ESTIMATE)
    assert "🧮 Оценка" in prompt and "<code>12000, 5</code>" in prompt
    listed = format_offer_list([(p, 50)], "Europe/Moscow", None, [ESTIMATE])
    assert "🧮 12 000 ₽ / 5 дней" in listed


def test_draft_with_estimate_says_so():
    d = Draft("Стоимость: 12 000 ₽.", "builtin", False, False)
    assert "оценка бота" in format_draft(d, estimated=True)
    assert "оценка бота" not in format_draft(d)


def test_list_marks_today_and_yesterday_by_moscow_date(make_project):
    now = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)  # в Москве уже 25.10 01:30
    tz = "Europe/Moscow"
    assert fmt_when(datetime(2026, 10, 24, 21, 10, tzinfo=UTC), tz, now) == "сегодня 00:10"
    assert fmt_when(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), tz, now) == "вчера 23:00"
    assert fmt_when(datetime(2026, 10, 23, 9, 0, tzinfo=UTC), tz, now) == "23.10.2026 12:00 МСК"
    assert fmt_when(datetime(2026, 10, 24, 21, 10, tzinfo=UTC), tz) == "25.10.2026 00:10 МСК"
    p = make_project(published_at=datetime(2026, 10, 24, 21, 10, tzinfo=UTC))
    out = format_offer_list([(p, 50)], tz, now=now)
    assert "свежие сверху" in out and "сегодня 00:10" in out


def test_selection_reason_names_directions_and_terms() -> None:
    r = _result(
        directions=["telegram", "backend", "own"],
        reasons=[
            "Направление «telegram»: telegram, бот для",
            "Направление «backend»: python",
            "Бюджет: 5 000 ₽",
        ],
    )
    assert selection_reason(r) == (
        "Telegram-боты (telegram, бот для); backend на Python и API (python); own"
    )
    long = _result(directions=["backend"], reasons=["Направление «backend»: " + "api, " * 60])
    assert selection_reason(long).endswith("…") and len(selection_reason(long)) <= 120


def test_offer_list_shows_reason_and_work24_offer_has_tips(make_project) -> None:
    p = make_project(platform="work24", url="https://work24.ru/order/1-bot")
    out = format_offer_list([(p, 70)], "Europe/Moscow", reasons=["Telegram-боты (бот для)"])
    assert "🎯 Telegram-боты (бот для)" in out and "Work24" in out
    offer = format_offer(p, Draft("Текст", "llm", False, False), [], 5000, 3)
    assert "штрафует на 500 ₽" in offer


def test_filters_show_platform_own_max_age():
    from dataclasses import replace

    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    sources = {
        p: replace(src, max_age_hours=72 if p == "superjob" else None)
        for p, src in s.sources.items()
    }
    out = format_filters(replace(s, max_age_hours=6, sources=sources))
    assert "макс. возраст проекта: 6 ч, SuperJob: 72 ч" in out
