"""Один цикл работы: сбор → дедупликация → отбор → режим → черновик → уведомление."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from freelance_bot.config import Settings
from freelance_bot.drafting import DraftGenerator, fill_estimate, fill_offer
from freelance_bot.http import BlockedError, SourceError
from freelance_bot.models import Draft, Platform, Project, ScoreResult
from freelance_bot.policy import SENDERS, Sender, decide_mode
from freelance_bot.pricing import Estimate, estimate
from freelance_bot.scoring import score_project
from freelance_bot.sources import Source
from freelance_bot.storage import Storage
from freelance_bot.summary import BuiltinSummarizer, CachedSummarizer
from freelance_bot.telegram_ui import (
    PLATFORM_NAMES,
    card_keyboard,
    esc,
    format_card,
    format_draft,
)

log = logging.getLogger(__name__)

FAILURES_BEFORE_ALERT = 3
BLOCK_PAUSE = timedelta(hours=1)  # история 8: «пауза (1 час)»
PAID_NOTE = "платный отклик"
OFFER_WINDOW = timedelta(days=7)  # /last: заказы FL обычно открыты несколько дней
OFFER_CLOSED = ("applied", "skipped", "sent")
OWN_OFFER_FLAG = "offer:{platform}:{external_id}"  # "цена,срок" — указанные пользователем

Keyboard = list[list[tuple[str, str]]]


class Notifier(Protocol):
    async def send_card(self, text: str, keyboard: Keyboard) -> None: ...

    async def send_text(self, text: str) -> None: ...


class ConsoleNotifier:
    """Печатает сообщения в консоль (dry-run без токена): HTML превращается в текст."""

    async def send_card(self, text: str, keyboard: Keyboard) -> None:
        print(_plain(text))
        if keyboard:
            print("  [" + "] [".join(label for row in keyboard for label, _ in row) + "]")
        print("-" * 60)

    async def send_text(self, text: str) -> None:
        print(_plain(text))
        print("-" * 60)


def _plain(text: str) -> str:
    text = re.sub(r'<a href="([^"]*)">([^<]*)</a>', r"\2: \1", text)
    return html.unescape(re.sub(r"</?[a-z]+>", "", text))


@dataclass(frozen=True)
class Offer:
    """Отклик для показа: текст, значения полей формы и чьи они (пользователя или оценка)."""

    project: Project
    draft: Draft
    price_rub: int
    days: int
    own: bool
    variant: int = 0  # 0 — первый текст, 1 и дальше — кнопка «Другой вариант»


@dataclass
class CycleReport:
    fetched: int = 0
    new: int = 0
    passed: int = 0
    notified: int = 0
    queued: int = 0  # ждут дайджеста
    errors: list[str] = field(default_factory=list)

    def add(self, other: CycleReport) -> None:
        self.fetched += other.fetched
        self.new += other.new
        self.passed += other.passed
        self.notified += other.notified
        self.queued += other.queued
        self.errors.extend(other.errors)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        sources: list[Source],
        drafter: DraftGenerator,
        notifier: Notifier,
        senders: Mapping[Platform, Sender] = SENDERS,
        *,
        dry_run: bool | None = None,
        clock: Callable[[], datetime] | None = None,
        summarizer: CachedSummarizer | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.sources = sources
        self.drafter = drafter
        self.notifier = notifier
        self.senders = senders
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self.summarizer = summarizer or CachedSummarizer(BuiltinSummarizer(), storage)

    # ── цикл ─────────────────────────────────────────────────────────────────

    async def run_once(self, platform: Platform | None = None) -> CycleReport:
        report = CycleReport()
        async with self._lock:
            if self.storage.get_flag("paused") == "1":
                return report
            for source in self.sources:
                if platform is not None and source.platform != platform:
                    continue
                report.add(await self._run_source(source))
        return report

    async def _run_source(self, source: Source) -> CycleReport:
        report = CycleReport()
        platform = source.platform
        name = PLATFORM_NAMES[platform]
        now = self._clock()

        blocked_until = self.storage.get_flag(f"blocked_until:{platform}")
        if blocked_until and datetime.fromisoformat(blocked_until) > now:
            return report

        try:
            projects = await source.fetch()
        except BlockedError as exc:
            until = now + BLOCK_PAUSE
            self.storage.set_flag(f"blocked_until:{platform}", until.isoformat())
            self.storage.set_flag(f"last_error:{platform}", str(exc))
            report.errors.append(f"{name}: {exc}")
            await self.notifier.send_text(
                f"⛔ {esc(name)}: похоже на блокировку или CAPTCHA ({esc(exc)}). "
                f"Сбор с этой площадки приостановлен на {BLOCK_PAUSE // timedelta(hours=1)} ч."
            )
            return report
        except Exception as exc:  # источник не должен ронять программу
            message = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
            log.warning("%s: ошибка сбора: %s", name, message)
            report.errors.append(f"{name}: {message}")
            await self._register_failure(platform, name, message)
            return report

        self.storage.set_flag(f"last_ok:{platform}", now.isoformat())
        self.storage.set_flag(f"last_error:{platform}", "")
        self.storage.set_flag(f"fail_count:{platform}", "0")
        report.fetched = len(projects)

        bootstrap = self.storage.get_flag(f"bootstrapped:{platform}") is None
        cutoff = now - timedelta(hours=self.settings.max_age_for(platform))
        candidates: list[tuple[Project, ScoreResult]] = []
        for p in projects:
            if not self.storage.upsert_project(p):
                continue
            report.new += 1
            if bootstrap and p.published_at < cutoff:
                self.storage.set_status(p.platform, p.external_id, "filtered_out", "bootstrap")
                continue
            result = score_project(p, self.settings, now)
            self.storage.save_score(p.platform, p.external_id, result)
            if not result.passed:
                note = result.excluded_reason or f"score {result.score} < {self.settings.min_score}"
                self.storage.set_status(p.platform, p.external_id, "filtered_out", note)
                continue
            if self._paid_response_skipped(p):
                self.storage.set_status(p.platform, p.external_id, "filtered_out", PAID_NOTE)
                continue
            report.passed += 1
            candidates.append((p, result))
        if bootstrap:
            self.storage.set_flag(f"bootstrapped:{platform}", "1")

        if self.settings.digest_enabled:  # придут списком в ближайший дайджест (digest_items)
            for p, _ in candidates:
                self.storage.set_status(p.platform, p.external_id, "queued")
            report.queued = len(candidates)
            return report
        candidates.sort(key=lambda item: item[1].score, reverse=True)
        for p, result in candidates:
            try:
                await self._deliver(p, result)
                report.notified += 1
            except Exception as exc:
                log.exception("не удалось обработать %s:%s", p.platform, p.external_id)
                report.errors.append(f"{p.platform}:{p.external_id}: {type(exc).__name__}")
                self.storage.set_status(p.platform, p.external_id, "failed", type(exc).__name__)
        return report

    def _paid_response_skipped(self, p: Project) -> bool:
        """FL без метки «для всех» — отклик платный (docs/platforms.md, §1); бриф: только
        бесплатные отклики. Присылаем такие, только если пользователь выключил настройку."""
        if p.platform != "fl" or p.extra.get("for_all"):
            return False
        return self.settings.sources["fl"].only_free_responses

    async def _register_failure(self, platform: Platform, name: str, message: str) -> None:
        self.storage.set_flag(f"last_error:{platform}", message)
        count = int(self.storage.get_flag(f"fail_count:{platform}") or "0") + 1
        self.storage.set_flag(f"fail_count:{platform}", str(count))
        if count == FAILURES_BEFORE_ALERT:  # ровно одно сообщение на серию
            await self.notifier.send_text(
                f"⚠ Источник {esc(name)} недоступен {count} раза подряд: {esc(message)}. "
                "Бот продолжит попытки."
            )

    # ── один проект ──────────────────────────────────────────────────────────

    async def _deliver(self, p: Project, result: ScoreResult) -> None:
        pid = (p.platform, p.external_id)
        sender = self.senders.get(p.platform)
        decision = decide_mode(p, self.settings, self.storage, sender, None, self.dry_run)
        draft: Draft | None = None
        if decision.requested != "notify":
            draft = await self.drafter.generate(p, result, 0)
            if decision.requested == "auto":
                decision = decide_mode(p, self.settings, self.storage, sender, draft, self.dry_run)

        summary = await self.summarizer.summarize(p)
        guess = estimate(p, result, self.settings)
        await self.notifier.send_card(
            format_card(p, result, decision, self.settings.timezone, summary, guess),
            card_keyboard(p.platform, p.external_id),
        )
        self.storage.set_status(*pid, "notified")
        if draft is None:
            return
        shown = self._with_estimate(draft, guess.price_rub, guess.days)
        await self.notifier.send_text(format_draft(*shown))
        reason = "; ".join(decision.reasons) or None
        self.storage.set_status(*pid, "drafted")

        if decision.send and sender is not None:
            await self._send(p, sender, draft, reason)
            return
        self.storage.add_response(
            *pid, decision.effective, draft.text, draft.generator, False, reason
        )

    @staticmethod
    def _with_estimate(draft: Draft, price_rub: int, days: int) -> tuple[Draft, bool]:
        """Черновик для показа: метки [ЦЕНА]/[СРОК] заполнены оценкой бота.

        В базе остаётся черновик с метками — туда кнопка «Цена и срок» впишет цену
        пользователя (fill_offer). Второе значение — была ли подставлена оценка."""
        if not (draft.price_placeholder or draft.deadline_placeholder):
            return draft, False
        text = fill_estimate(draft.text, price_rub, days)
        return Draft(text, draft.generator, False, False), True

    async def _send(self, p: Project, sender: Sender, draft: Draft, reason: str | None) -> None:
        pid = (p.platform, p.external_id)
        price, days = self.settings.offer_price_rub, self.settings.offer_days
        assert price is not None and days is not None  # гарантирует decide_mode
        try:
            await sender.send(p, draft.text, price, days)
        except Exception as exc:
            self.storage.add_response(
                *pid, "draft", draft.text, draft.generator, False, "автоотправка не удалась"
            )
            self.storage.set_status(*pid, "failed", f"отправка: {type(exc).__name__}")
            await self.notifier.send_text(
                f"⚠ Автоотправка не удалась ({esc(type(exc).__name__)}): "
                f"{esc(p.title)}. Черновик выше — отправьте вручную."
            )
            return
        self.storage.add_response(*pid, "auto", draft.text, draft.generator, True, reason)
        self.storage.set_status(*pid, "sent")

    # ── кнопка «Другой вариант» ──────────────────────────────────────────────

    async def redraft(self, platform: Platform | str, external_id: str) -> Offer | None:
        """Новый вариант отклика — в том же виде, что выбранный заказ (`response`): текст с
        ценой и сроком пользователя или оценкой бота, номер варианта."""
        p = self.storage.get_project(platform, external_id)
        result = self.storage.get_score(platform, external_id)
        if p is None or result is None:
            return None
        key = f"variant:{platform}:{external_id}"
        variant = int(self.storage.get_flag(key) or "0") + 1
        self.storage.set_flag(key, str(variant))
        draft = await self.drafter.generate(p, result, variant)
        self.storage.add_response(
            platform, external_id, "draft", draft.text, draft.generator, False, "другой вариант"
        )
        price, days, own = self._offer_values(p)
        shown = Draft(fill_estimate(draft.text, price, days), draft.generator, False, False)
        return Offer(p, shown, price, days, own, variant)

    # ── цена и срок от пользователя ──────────────────────────────────────────

    def estimate(self, p: Project) -> Estimate:
        """Оценка цены и срока для /last и вопроса о цене."""
        return estimate(p, self._score(p), self.settings)

    def digest_items(self) -> list[tuple[Project, ScoreResult]]:
        """Новые заказы, ещё не присланные (статус queued), свежие первыми — как в /last."""
        since = self._clock() - OFFER_WINDOW
        items = [(p, self._score(p)) for p in self.storage.projects_with_status("queued", since)]
        items.sort(key=lambda item: (item[0].published_at, item[1].score), reverse=True)
        return items

    def mark_notified(self, projects: list[Project]) -> None:
        """Дайджест доставлен: больше эти заказы как новые не присылаются."""
        for p in projects:
            self.storage.set_status(p.platform, p.external_id, "notified", "дайджест")

    def offer_candidates(self, limit: int = 10) -> list[tuple[Project, ScoreResult]]:
        """Подходящие заказы за OFFER_WINDOW, на которые ещё не откликались: свежие первыми,
        при равном времени — релевантнее (решение пользователя: свежесть важнее балла).

        В отличие от карточек, сюда попадают и проекты, скрытые bootstrap или по возрасту:
        балл считается на момент публикации, чтобы возраст не отсекал заказ."""
        since = self._clock() - OFFER_WINDOW
        items = []
        for p in self.storage.recent_projects(since, OFFER_CLOSED):
            result = score_project(p, self.settings, p.published_at)
            if result.passed and not self._paid_response_skipped(p):
                items.append((p, result))
        items.sort(key=lambda item: (item[0].published_at, item[1].score), reverse=True)
        return items[:limit]

    async def response(self, platform: Platform | str, external_id: str) -> Offer | None:
        """Готовый отклик на выбранный заказ: последний черновик (нет — новый) с ценой и
        сроком пользователя, если он их указывал, иначе с оценкой бота."""
        p = self.storage.get_project(platform, external_id)
        if p is None:
            return None
        latest = self.storage.latest_response(platform, external_id)
        if latest is None:
            draft = await self.drafter.generate(p, self._score(p), 0)
            self.storage.add_response(
                platform, external_id, "draft", draft.text, draft.generator, False, "выбран заказ"
            )
            latest = (draft.text, draft.generator)
        text, generator = latest
        price, days, own = self._offer_values(p)
        return Offer(p, Draft(fill_estimate(text, price, days), generator, False, False),
                     price, days, own)

    async def offer(
        self, platform: Platform | str, external_id: str, price_rub: int, days: int
    ) -> Offer | None:
        """Последний черновик проекта (нет — новый) с ценой и сроком пользователя.

        Ничего не отправляет на биржу: отклик пользователь вставляет сам (ADR 0001)."""
        p = self.storage.get_project(platform, external_id)
        if p is None:
            return None
        latest = self.storage.latest_response(platform, external_id)
        if latest is None:
            draft = await self.drafter.generate(p, self._score(p), 0)
            latest = (draft.text, draft.generator)
        text, generator = latest
        text = fill_offer(text, price_rub, days)
        self.storage.add_response(
            platform, external_id, "draft", text, generator, False, "цена и срок"
        )
        flag = OWN_OFFER_FLAG.format(platform=platform, external_id=external_id)
        self.storage.set_flag(flag, f"{price_rub},{days}")
        return Offer(p, Draft(text, generator, False, False), price_rub, days, True)

    def _score(self, p: Project) -> ScoreResult:
        return self.storage.get_score(p.platform, p.external_id) or score_project(
            p, self.settings, p.published_at
        )

    def _offer_values(self, p: Project) -> tuple[int, int, bool]:
        """Цена, срок и «указаны пользователем» — иначе оценка бота."""
        own = self.storage.get_flag(
            OWN_OFFER_FLAG.format(platform=p.platform, external_id=p.external_id)
        )
        if own:
            price, days = own.split(",")
            return int(price), int(days), True
        guess = self.estimate(p)
        return guess.price_rub, guess.days, False
