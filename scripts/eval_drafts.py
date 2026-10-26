"""Оценка черновиков откликов по эталонам `profile/references/` (RUBRIC.md).

Прогоняет генератор (встроенный или нейросеть) на входах из `examples.jsonl` и `eval.jsonl` и
проверяет то, что проверяется автоматически: длину, один вопрос, подпись, цену и срок из входа,
валидатор, контакты, неподтверждённый опыт, фразы `must_not_include`, честную границу опыта,
ручную оценку там, где её ждут. Тексты — в отчёт (`--out`), их стоит прочитать глазами:
суть задачи и план автоматически не оценить.

`eval.jsonl` — только для измерения: скрипт проверяет, что в промпт нейросети не попал ни один
проверочный случай, кроме самого задания.

    .venv/bin/python scripts/eval_drafts.py                       # встроенный генератор
    .venv/bin/python scripts/eval_drafts.py --llm --pause 25      # нейросеть по ключу из .env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from freelance_bot.config import load_settings  # noqa: E402
from freelance_bot.drafting import (  # noqa: E402
    REVIEW_GENERATOR,
    BuiltinDrafter,
    LlmDrafter,
    unconfirmed_tech,
    validate_draft,
)
from freelance_bot.models import Project  # noqa: E402
from freelance_bot.profile import load_profile  # noqa: E402
from freelance_bot.scoring import score_project  # noqa: E402

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
PLACEHOLDERS = {"[ЦЕНА]", "[СРОК]"}
_RUB_RE = re.compile(r"(\d[\d\s]*)\s*₽")
_DAYS_RE = re.compile(r"(\d+)\s*(?:дн|день|дня|дней)")
_PLACEHOLDER_RE = re.compile(r"\[(?!ЦЕНА\]|СРОК\])[^\[\]\n]{1,200}\]")  # метки, кроме цены и срока
_CONTACT_RE = re.compile(r"\+?\d[\d\s\-()]{9,}\d|@[A-Za-z0-9_]{3,}|[\w.+-]+@[\w-]+\.\w+|https?://")
_BOUNDARY_RE = re.compile(
    r"не\s+подтвержд|не\s+заявлен|не\s+перечисл|нет\s+подтвержд|не\s+работал|не\s+делал"
    r"|опыта\s+[^.]{0,40}\s+нет|без\s+опыта",
    re.IGNORECASE,
)


def load_cases(refs: Path) -> list[dict[str, Any]]:
    cases = []
    for name in ("examples.jsonl", "eval.jsonl"):
        for line in (refs / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append({**json.loads(line), "kind": name.split(".")[0]})
    return cases


def project_for(case: dict[str, Any]) -> Project:
    given = case["input"]
    return Project(
        platform=given["platform"], external_id=case["id"],
        url=f"https://example.invalid/{case['id']}", title=given["title"],
        description=given["brief"], published_at=NOW, budget_min=None, budget_max=None,
        offers_count=None, category=None,
    )  # fmt: skip


def offer_settings(settings: Any, given: dict[str, Any]) -> Any:
    """Цена и срок входа — в настройки, как их подставил бы владелец; метки — остаются."""
    price = None if given["price"] in PLACEHOLDERS else int(re.sub(r"\D", "", given["price"]))
    days = None if given["deadline"] in PLACEHOLDERS else int(re.sub(r"\D", "", given["deadline"]))
    return replace(settings, offer_price_rub=price, offer_days=days)


def boundary_terms(refs: Path, profile: Any) -> set[str]:
    """Технологии из «not_confirmed_by_resume» (facts.json), которых нет в фактах профиля:
    встретилась в ТЗ — отклик должен честно сказать, что опыта нет."""
    facts = json.loads((refs / "facts.json").read_text(encoding="utf-8"))
    words = unconfirmed_tech("\n".join(facts.get("not_confirmed_by_resume", [])), profile)
    return {w.lower() for w in words}


def checks(
    case: dict[str, Any], text: str, generator: str, profile: Any, boundary: set[str]
) -> dict[str, bool]:
    """Пункт рубрики → прошёл. Пустой текст ручной оценки проверяется только как ручная оценка."""
    given = case["input"]
    expects_review = "needs_review" in case.get("expected_behavior", "")
    result: dict[str, bool] = {"ручная оценка там, где ждут": (generator == REVIEW_GENERATOR)
                               == expects_review}  # fmt: skip
    if generator == REVIEW_GENERATOR:
        return result
    must_not = [m for m in case.get("must_not_include", []) if " " in m or len(m) > 8]
    brief = f"{given['title']}\n{given['brief']}"
    gaps = [w for w in unconfirmed_tech(brief, profile) if w.lower() in boundary]
    result.update({
        "500–900 знаков": 500 <= len(_PLACEHOLDER_RE.sub("", text)) <= 900,
        "ровно один вопрос": text.count("?") == 1,
        "подпись — только имя": text.rstrip().endswith(f"\n{profile.name}"),
        "цена из входа": given["price"] in text,
        "срок из входа": given["deadline"] in text,
        "валидатор": not validate_draft(text, profile, given["client_requirements"]),
        "без контактов": not _CONTACT_RE.search(text) and "телефон" not in text.lower(),
        "нет фраз must_not_include": not any(m.lower() in text.lower() for m in must_not),
        "граница опыта названа": not gaps or bool(_BOUNDARY_RE.search(text)),
    })  # fmt: skip
    return result


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--refs", type=Path, default=ROOT / "profile/references")
    parser.add_argument("--profile", type=Path, default=ROOT / "profile/profile.toml")
    parser.add_argument("--llm", action="store_true", help="нейросеть по ключу из .env")
    parser.add_argument("--pause", type=float, default=0, help="пауза между заданиями, с")
    parser.add_argument("--out", type=Path, help="куда записать тексты и проверки (JSON)")
    parser.add_argument("--ideal", action="store_true",
                        help="проверить сами эталонные ответы (калибровка проверок)")
    args = parser.parse_args(argv)
    if not (args.refs / "eval.jsonl").exists():
        print(f"нет эталонов в {args.refs}", file=sys.stderr)
        return 2

    settings = load_settings(ROOT / "settings.toml", ROOT / ".env")
    profile = load_profile(args.profile)
    cases = load_cases(args.refs)
    held_out = [c for c in cases if c["kind"] == "eval"]
    boundary = boundary_terms(args.refs, profile)
    report, failed = [], 0
    for i, case in enumerate(cases):
        if i and args.pause:
            await asyncio.sleep(args.pause)
        own = offer_settings(settings, case["input"])
        p = project_for(case)
        r = replace(score_project(p, own, NOW),
                    client_requirements=list(case["input"]["client_requirements"]))  # fmt: skip
        if args.ideal:
            if "ideal_response" not in case:
                continue
            got = checks(case, case["ideal_response"], "ideal", profile, boundary)
            bad = [name for name, ok in got.items() if not ok]
            failed += bool(bad)
            print(f"{'✅' if not bad else '❌'} {case['id']:<26} эталон"
                  + (f"  — {', '.join(bad)}" if bad else ""))  # fmt: skip
            report.append({"id": case["id"], "generator": "ideal", "failed": bad})
            continue
        if args.llm:
            drafter: Any = LlmDrafter(profile, own, storage=None)
            prompt = drafter._prompt(p, r, 0)
            leaked = [c["id"] for c in held_out
                      if c["id"] != case["id"] and c["input"]["brief"] in prompt]  # fmt: skip
            leaked += [c["id"] for c in held_out if c["expected_behavior"] in prompt]
            if leaked:
                print(f"СТОП: в промпт попали проверочные случаи {leaked}", file=sys.stderr)
                return 3
        else:
            drafter = BuiltinDrafter(profile, own)
        draft = await drafter.generate(p, r, 0)
        got = checks(case, draft.text, draft.generator, profile, boundary)
        bad = [name for name, ok in got.items() if not ok]
        failed += bool(bad)
        mark = "✅" if not bad else "❌"
        print(f"{mark} {case['id']:<26} {draft.generator:<7} {len(draft.text):>4} зн."
              + (f"  — {', '.join(bad)}" if bad else ""))  # fmt: skip
        report.append({"id": case["id"], "generator": draft.generator, "text": draft.text,
                       "failed": bad})  # fmt: skip
    print(f"\nбез замечаний: {len(report or cases) - failed} из {len(report or cases)}")
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
