"""decide_mode: auto отправляет только при всех подтверждённых условиях, иначе draft с причиной."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from freelance_bot.config import Settings, SourceSettings, load_settings
from freelance_bot.models import Draft, Project
from freelance_bot.policy import SENDERS, decide_mode
from freelance_bot.storage import Storage


class FakeSender:
    def __init__(
        self, platform: str = "fl", official_confirmed: bool = True, cost: int | None = 0
    ) -> None:
        self.platform = platform
        self.official_confirmed = official_confirmed
        self.response_cost_rub = cost
        self.sent: list[str] = []

    async def send(self, p: Project, text: str, price_rub: int, days: int) -> None:
        self.sent.append(text)


READY_DRAFT = Draft(
    text="готовый отклик", generator="builtin", price_placeholder=False, deadline_placeholder=False
)


@pytest.fixture
def storage() -> Iterator[Storage]:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def auto_settings(repo_root: Path, tmp_path: Path) -> Settings:
    """Все условия auto выполнены: цена, срок, лимиты > 0, не dry-run."""
    base = load_settings(repo_root / "settings.toml", tmp_path / "no.env")
    sources = {
        name: dataclasses.replace(src, mode="auto", daily_auto_limit=3)
        for name, src in base.sources.items()
    }
    return dataclasses.replace(
        base,
        sources=sources,
        offer_price_rub=20000,
        offer_days=5,
        kwork_connects_daily_limit=2,
        dry_run=False,
    )


def _with_source(settings: Settings, platform: str, **changes) -> Settings:
    sources: dict = dict(settings.sources)
    src: SourceSettings = sources[platform]
    sources[platform] = dataclasses.replace(src, **changes)
    return dataclasses.replace(settings, sources=sources)


def _sent_response(storage: Storage, platform: str, n: int) -> None:
    for i in range(n):
        storage.add_response(platform, f"sent{i}", "auto", "текст", "builtin", True, None)


def test_senders_registry_is_empty() -> None:
    assert SENDERS == {}


@pytest.mark.parametrize("platform", ["fl", "kwork"])
def test_auto_sends_only_when_every_condition_holds(
    make_project, auto_settings: Settings, storage: Storage, platform: str
) -> None:
    sender = FakeSender(platform)
    d = decide_mode(
        make_project(platform=platform), auto_settings, storage, sender, READY_DRAFT, False
    )
    assert (d.requested, d.effective, d.send, d.reasons) == ("auto", "auto", True, [])


Setup = Callable[[Settings, Storage], tuple[Settings, FakeSender | None, Draft | None, bool]]

VIOLATIONS: dict[str, tuple[str, Setup, str]] = {
    "нет отправщика": (
        "fl",
        lambda s, st: (s, None, READY_DRAFT, False),
        "нет подтверждённого способа отправки",
    ),
    "не official_confirmed": (
        "fl",
        lambda s, st: (s, FakeSender(official_confirmed=False), READY_DRAFT, False),
        "не подтверждён",
    ),
    "стоимость неизвестна": (
        "fl",
        lambda s, st: (s, FakeSender(cost=None), READY_DRAFT, False),
        "стоимость отклика неизвестна",
    ),
    "отклик платный": ("fl", lambda s, st: (s, FakeSender(cost=50), READY_DRAFT, False), "платный"),
    "нет цены": (
        "fl",
        lambda s, st: (
            dataclasses.replace(s, offer_price_rub=None),
            FakeSender(),
            READY_DRAFT,
            False,
        ),
        "offer.price_rub",
    ),
    "нет срока": (
        "fl",
        lambda s, st: (dataclasses.replace(s, offer_days=None), FakeSender(), READY_DRAFT, False),
        "offer.days",
    ),
    "дневной лимит 0": (
        "fl",
        lambda s, st: (_with_source(s, "fl", daily_auto_limit=0), FakeSender(), READY_DRAFT, False),
        "daily_auto_limit",
    ),
    "дневной лимит исчерпан": (
        "fl",
        lambda s, st: (_sent_response(st, "fl", 3), (s, FakeSender(), READY_DRAFT, False))[1],
        "исчерпан",
    ),
    "лимит коннектов Kwork 0": (
        "kwork",
        lambda s, st: (
            dataclasses.replace(s, kwork_connects_daily_limit=0),
            FakeSender("kwork"),
            READY_DRAFT,
            False,
        ),
        "kwork.connects_daily_limit",
    ),
    "коннекты Kwork исчерпаны": (
        "kwork",
        lambda s, st: (
            _sent_response(st, "kwork", 2),
            (s, FakeSender("kwork"), READY_DRAFT, False),
        )[1],
        "коннект",
    ),
    "dry-run флагом": ("fl", lambda s, st: (s, FakeSender(), READY_DRAFT, True), "dry-run"),
    "dry-run в настройках": (
        "fl",
        lambda s, st: (dataclasses.replace(s, dry_run=True), FakeSender(), READY_DRAFT, False),
        "dry-run",
    ),
    "метки в черновике": (
        "fl",
        lambda s, st: (
            s,
            FakeSender(),
            dataclasses.replace(READY_DRAFT, price_placeholder=True, deadline_placeholder=True),
            False,
        ),
        "[ЦЕНА]",
    ),
}


@pytest.mark.parametrize("case", sorted(VIOLATIONS))
def test_each_violation_downgrades_auto_to_draft_with_reason(
    make_project, auto_settings: Settings, storage: Storage, case: str
) -> None:
    platform, setup, expected_reason = VIOLATIONS[case]
    settings, sender, draft, dry_run = setup(auto_settings, storage)
    d = decide_mode(make_project(platform=platform), settings, storage, sender, draft, dry_run)
    assert d.requested == "auto"
    assert d.effective == "draft"
    assert d.send is False
    assert any(expected_reason in r for r in d.reasons), d.reasons


def test_auto_never_sends_without_draft_or_with_foreign_sender(
    make_project, auto_settings: Settings, storage: Storage
) -> None:
    d = decide_mode(make_project(), auto_settings, storage, FakeSender(), None, False)
    assert (d.effective, d.send, d.reasons) == ("auto", False, [])  # нечего отправлять
    d = decide_mode(make_project(), auto_settings, storage, FakeSender("kwork"), READY_DRAFT, False)
    assert d.send is False and d.effective == "draft"
    assert "отправщик для другой площадки (kwork)" in d.reasons


def test_daily_limit_counts_only_its_platform(
    make_project, auto_settings: Settings, storage: Storage
) -> None:
    _sent_response(storage, "kwork", 3)
    d = decide_mode(make_project(), auto_settings, storage, FakeSender(), READY_DRAFT, False)
    assert d.send is True


def test_kwork_connects_limit_zero_never_sends_even_with_everything_else(
    make_project, auto_settings: Settings, storage: Storage
) -> None:
    settings = dataclasses.replace(auto_settings, kwork_connects_daily_limit=0)
    settings = _with_source(settings, "kwork", daily_auto_limit=100)
    sender = FakeSender("kwork")
    d = decide_mode(make_project(platform="kwork"), settings, storage, sender, READY_DRAFT, False)
    assert d.send is False and d.effective == "draft"


async def test_dry_run_never_calls_sender(
    make_project, auto_settings: Settings, storage: Storage
) -> None:
    sender = FakeSender()
    d = decide_mode(make_project(), auto_settings, storage, sender, READY_DRAFT, True)
    assert d.send is False
    assert sender.sent == []


@pytest.mark.parametrize("mode", ["notify", "draft"])
def test_notify_and_draft_modes_are_kept_and_never_send(
    make_project, auto_settings: Settings, storage: Storage, mode: str
) -> None:
    settings = _with_source(auto_settings, "fl", mode=mode)
    d = decide_mode(make_project(), settings, storage, FakeSender(), READY_DRAFT, False)
    assert (d.requested, d.effective, d.send, d.reasons) == (mode, mode, False, [])


def test_several_violations_are_all_reported(
    make_project, auto_settings: Settings, storage: Storage
) -> None:
    settings = dataclasses.replace(auto_settings, offer_price_rub=None, offer_days=None)
    d = decide_mode(make_project(), settings, storage, None, None, True)
    assert d.effective == "draft"
    assert len(d.reasons) >= 4


def test_no_purchase_or_payment_calls_in_policy_and_drafting(repo_root: Path) -> None:
    call = re.compile(r"\b(buy|purchase|pay|subscribe|checkout)\w*\s*\(", re.IGNORECASE)
    for name in ("policy.py", "drafting.py"):
        source = (repo_root / "freelance_bot" / name).read_text(encoding="utf-8")
        assert not call.search(source), name
