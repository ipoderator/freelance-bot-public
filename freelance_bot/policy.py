"""Режимы notify/draft/auto и защита автоотправки."""

from __future__ import annotations

from typing import Protocol

from freelance_bot.config import Settings
from freelance_bot.models import Draft, ModeDecision, Platform, Project
from freelance_bot.storage import Storage


class Sender(Protocol):
    platform: Platform
    official_confirmed: bool
    response_cost_rub: int | None

    async def send(self, p: Project, text: str, price_rub: int, days: int) -> None: ...


SENDERS: dict[Platform, Sender] = {}


def decide_mode(
    p: Project,
    settings: Settings,
    storage: Storage,
    sender: Sender | None,
    draft: Draft | None,
    dry_run: bool,
) -> ModeDecision:
    """Какой режим применить к проекту. Чистая функция: ничего не отправляет.

    `auto` → `send=True` только если выполнены все условия; любое нарушение
    переводит в `draft` и попадает в `reasons` (по-русски, для карточки).
    `draft is None` (черновик ещё не готов) нарушением не считается — режим остаётся
    `auto`, но `send=False`: отправлять нечего, решение перепроверяется с черновиком.
    """
    source = settings.sources[p.platform]
    requested = source.mode
    if requested != "auto":
        return ModeDecision(requested=requested, effective=requested, reasons=[], send=False)

    reasons: list[str] = []
    if sender is None:
        reasons.append("нет подтверждённого способа отправки на эту площадку")
    else:
        if sender.platform != p.platform:
            reasons.append(f"отправщик для другой площадки ({sender.platform})")
        if sender.official_confirmed is not True:
            reasons.append("способ отправки не подтверждён как официальный")
        cost = sender.response_cost_rub
        if cost is None:
            reasons.append("стоимость отклика неизвестна")
        elif cost != 0:
            reasons.append(f"отклик платный ({cost} ₽) — платные отклики не отправляются")

    if settings.offer_price_rub is None:
        reasons.append("не задана цена (offer.price_rub в settings.toml)")
    if settings.offer_days is None:
        reasons.append("не задан срок (offer.days в settings.toml)")
    if draft is not None and (draft.price_placeholder or draft.deadline_placeholder):
        reasons.append("в черновике остались метки [ЦЕНА]/[СРОК]")

    limit = source.daily_auto_limit
    if limit <= 0:
        reasons.append(f"дневной лимит автоотправки sources.{p.platform}.daily_auto_limit = 0")
    else:
        sent_today = storage.count_auto_sent_today(p.platform)
        if sent_today >= limit:
            reasons.append(f"дневной лимит автоотправки исчерпан ({sent_today} из {limit})")

    if p.platform == "kwork":
        connects = settings.kwork_connects_daily_limit
        if connects <= 0:
            reasons.append("лимит коннектов Kwork kwork.connects_daily_limit = 0")
        else:
            used = storage.count_kwork_connects_today()
            if used >= connects:
                reasons.append(f"лимит коннектов Kwork на сегодня исчерпан ({used} из {connects})")

    if dry_run or settings.dry_run:
        reasons.append("dry-run: отправка на биржи отключена")

    if reasons:
        return ModeDecision(requested="auto", effective="draft", reasons=reasons, send=False)
    return ModeDecision(requested="auto", effective="auto", reasons=[], send=draft is not None)
