"""Telegram-каналы с вакансиями (sources/tgchannels.py, ADR 0016): разбор страницы t.me/s,
пропуск рекламы, без контактов, повторы между каналами. Сети нет — снятая страница канала
в fixtures (телефоны в ней заменены вымышленными)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_bot.config import ConfigError, load_settings
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.tgchannels import TgChannelsSource, parse_page, parse_post
from freelance_bot.summary import BUILTIN_ONLY

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "tests" / "fixtures" / "tgchannels_rabota_perm.html").read_text()
NOW = datetime(2026, 10, 25, 14, 0, tzinfo=UTC)
WHEN = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.urls = pages, []

    async def get_text(self, url, headers=None, expect=None):
        self.urls.append(url)
        return self.pages[url.rsplit("/", 1)[1]]


def test_page_gives_vacancies_and_skips_ads():
    """5 постов: 3 вакансии, реклама контрактной службы и букмекера — без «Обязанностей»."""
    found = parse_page(PAGE, "rabota_perm", "Пермь")
    assert [p.title for p in found] == [
        "Повар в столовую", "Продавец-консультант в зоомагазин", "Торговый представитель",
    ]  # fmt: skip
    cook = found[0]
    assert cook.platform == "tgchannels" and cook.url == "https://t.me/rabota_perm/10738"
    assert cook.published_at == datetime(2026, 10, 23, 12, 39, 24, tzinfo=UTC)
    assert cook.budget_min == cook.budget_max == 280000 and cook.extra["salary"] == "280 000 ₽"
    assert cook.extra["area"] == "Пермь" and cook.description.startswith("Обязанности:")
    assert found[2].budget_min is None
    assert found[2].extra["salary"] == "Зарплата по договоренности"


def test_contacts_are_not_copied():
    for p in parse_page(PAGE, "rabota_perm", "Пермь"):
        assert "Контакты" not in p.description and "7999" not in p.description
        assert "t.me" not in p.description


def test_inline_contacts_and_latin_letters():
    lines = [
        "Пекарь", "60000 - 70000 ₽", "Обязанности:",
        "• Пoдгoтoвкa теста, звоните +7 (912) 345-67-89 или 8 912 345 67 89",
        "• Пишите @hr_bakery, hr@bakery.example или https://bakery.example/job",
    ]  # fmt: skip
    p = parse_post(lines, "rabota_perm", "1", "Пермь", WHEN)
    assert p is not None and (p.budget_min, p.budget_max) == (60000, 70000)
    assert p.extra["salary"] == "60 000–70 000 ₽"
    assert "Подготовка теста" in p.description  # латиница в русском слове — кириллица
    for contact in ("912", "@hr_bakery", "bakery.example", "https"):
        assert contact not in p.description


def test_post_without_salary_line_and_not_a_vacancy():
    p = parse_post(["Грузчик", "Требования:", "• Без опыта"], "c", "1", "Пермь", WHEN)
    assert p is not None and p.extra == {"area": "Пермь"} and p.description.startswith("Треб")
    assert parse_post(["Служба по контракту", "Выплаты до 4 500 000 ₽"], "c", "2", "Пермь",
                      WHEN) is None  # fmt: skip


async def test_same_post_in_two_channels_is_one_vacancy():
    fetcher = FakeFetcher({"rabota_perm": PAGE, "vakansii_perm": PAGE.replace(
        "rabota_perm/", "vakansii_perm/")})  # fmt: skip
    source = TgChannelsSource(fetcher, {"rabota_perm": "Пермь", "vakansii_perm": "Пермь"},
                              max_age_hours=72, clock=lambda: NOW)  # fmt: skip
    found = await source.fetch()
    assert [p.title for p in found] == [  # свежие сверху; первый канал — ссылка на его пост
        "Торговый представитель", "Продавец-консультант в зоомагазин", "Повар в столовую",
    ]  # fmt: skip
    assert all(p.url.startswith("https://t.me/rabota_perm/") for p in found)
    assert fetcher.urls == ["https://t.me/s/rabota_perm", "https://t.me/s/vakansii_perm"]


async def test_old_posts_are_dropped():
    source = TgChannelsSource(FakeFetcher({"rabota_perm": PAGE}), {"rabota_perm": "Пермь"},
                              max_age_hours=12, clock=lambda: NOW)  # fmt: skip
    assert [p.title for p in await source.fetch()] == ["Торговый представитель"]


async def test_channel_without_posts_is_skipped_all_without_posts_is_error(caplog):
    empty = "<html><div class='tgme_channel_info'>Канал</div></html>"
    fetcher = FakeFetcher({"rabota_perm": PAGE, "gone_channel": empty})
    source = TgChannelsSource(fetcher, {"gone_channel": "Пермь", "rabota_perm": "Пермь"},
                              max_age_hours=72, clock=lambda: NOW)  # fmt: skip
    assert len(await source.fetch()) == 3
    assert "gone_channel" in caplog.text
    lonely = TgChannelsSource(fetcher, {"gone_channel": "Пермь"}, max_age_hours=72)
    with pytest.raises(SourceFormatError):
        await lonely.fetch()


def test_posts_never_go_to_llm():
    """Условия Telegram: собранное нельзя отдавать нейросети — суть только встроенная."""
    assert "tgchannels" in BUILTIN_ONLY


def test_channels_setting(tmp_path):
    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    channels = settings.sources["tgchannels"].channels
    assert channels["rabota_perm"] == "Пермь" and len(channels) == 18
    assert settings.sources["superjob"].channels == {}
    bad = tmp_path / "s.toml"
    bad.write_text('[sources.tgchannels.channels]\n"t.me/x" = "Пермь"\n')
    with pytest.raises(ConfigError, match="channels"):
        load_settings(bad, ROOT / "no-such.env")
    bad.write_text('[sources.superjob]\nchannels = { rabota_perm = "Пермь" }\n')
    with pytest.raises(ConfigError, match="только для sources.tgchannels"):
        load_settings(bad, ROOT / "no-such.env")
