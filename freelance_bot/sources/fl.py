"""FL.ru: официальный RSS проектов (только GET, без входа).

Поля item: title (бюджет в скобках «(Бюджет: 50 000 ₽)»), link, description (обрезано),
category, pubDate. Количества откликов в RSS нет — offers_count всегда None.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from freelance_bot.http import Fetcher
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

_ID_RE = re.compile(r"/projects/(\d+)")
# Хвост заголовка: «(Бюджет: 15 000 ₽)», «(Бюджет: 10 000 ₽, для всех)»,
# «(Бюджет: по договорённости)».
_BUDGET_TAIL_RE = re.compile(r"\(\s*Бюджет:\s*([^()]*)\)\s*$", re.IGNORECASE)
_FOR_ALL_TAIL_RE = re.compile(r"\(\s*для всех\s*\)\s*$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d[\d\s]*")
_TAGS_RE = re.compile(r"<[^>]+>")
FL_HOSTS = frozenset({"www.fl.ru", "fl.ru"})


def _project_url(link: str, external_id: str) -> str:
    """Ссылка из ленты, если это https на fl.ru; иначе каноническая по номеру проекта.

    Ссылка становится кнопкой «Открыть заказ на FL.ru» — чужой домен или javascript: туда
    попасть не должны (а не-http адрес Telegram в кнопке вообще отвергает)."""
    parts = urlsplit(link)
    if parts.scheme == "https" and parts.hostname in FL_HOSTS:
        return link
    return f"https://www.fl.ru/projects/{external_id}/"


def _clean(text: str | None) -> str:
    """Разэкранировать сущности (&#8381;), убрать NBSP и лишние пробелы."""
    text = html.unescape(text or "").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _split_budget(raw_title: str) -> tuple[str, int | None, bool]:
    """-> (заголовок без хвоста с бюджетом, бюджет в рублях или None, метка «для всех»)."""
    title = _clean(raw_title)
    budget: int | None = None
    for_all = False
    match = _BUDGET_TAIL_RE.search(title)
    if match:
        title = title[: match.start()].rstrip()
        inner = match.group(1)
        for_all = "для всех" in inner.lower()
        amount = _AMOUNT_RE.search(inner.split(",")[0])
        if amount:
            budget = int(re.sub(r"\s", "", amount.group(0)))
    match = _FOR_ALL_TAIL_RE.search(title)
    if match:
        title = title[: match.start()].rstrip()
        for_all = True
    return title, budget, for_all


def parse_fl_rss(xml: str) -> list[Project]:
    try:
        root = SafeET.fromstring(xml.lstrip("﻿").encode("utf-8"))
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise SourceFormatError(f"FL.ru: ответ не похож на RSS ({type(exc).__name__})") from exc
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise SourceFormatError("FL.ru: нет <rss><channel> в ответе")

    projects: list[Project] = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        id_match = _ID_RE.search(link)
        if not id_match:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        title, budget, for_all = _split_budget(item.findtext("title") or "")
        description = _clean(_TAGS_RE.sub(" ", item.findtext("description") or ""))
        category = _clean(item.findtext("category")) or None
        projects.append(
            Project(
                platform="fl",
                external_id=id_match.group(1),
                url=_project_url(link, id_match.group(1)),
                title=title,
                description=description,
                published_at=published.astimezone(UTC),
                budget_min=None,
                budget_max=budget,
                offers_count=None,
                category=category,
                extra={"for_all": True} if for_all else {},
            )
        )
    return projects


class FlSource:
    """Читает список RSS-URL из настроек, объединяет и дедуплицирует по external_id."""

    platform: Platform = "fl"

    def __init__(self, fetcher: Fetcher, urls: Sequence[str]) -> None:
        self._fetcher = fetcher
        self._urls = list(urls)

    async def fetch(self) -> list[Project]:
        merged: dict[str, Project] = {}
        for url in self._urls:
            text = await self._fetcher.get_text(url)
            for project in parse_fl_rss(text):
                merged.setdefault(project.external_id, project)
        return list(merged.values())
