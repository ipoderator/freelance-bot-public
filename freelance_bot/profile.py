"""Профиль исполнителя для черновиков: подтверждённый опыт отдельно от направлений."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ProfileError(Exception):
    """Ошибка в profile/profile.toml: текст понятен пользователю."""


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    tags: list[str]
    source: str  # дословная строка резюме (profile/resume_facts.txt)


@dataclass(frozen=True)
class Profile:
    name: str
    confirmed_experience: list[Fact]
    target_directions: list[str]
    forbidden_claims: list[str] = field(default_factory=list)
    contacts: dict[str, str] = field(default_factory=dict)
    include_contacts: bool = False
    # Подтверждённый стаж разработки, лет: больший стаж в отклике — ложь (validate_draft)
    experience_years: float = 0.0
    # Делал ли уже Telegram-ботов / AI-агентов: нет — утверждения «делал ботов» запрещены
    built_bots_or_agents: bool = False


def load_profile(path: Path = Path("profile/profile.toml")) -> Profile:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ProfileError(f"Файл профиля {path} не найден") from None
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"Файл профиля {path} — ошибка синтаксиса TOML: {exc}") from None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("profile: name — нужно имя для подписи (например, «Иван»)")

    contacts_table = raw.get("contacts", {})
    if not isinstance(contacts_table, dict):
        raise ProfileError("profile: [contacts] — ожидается раздел")
    include = contacts_table.get("include_in_responses", False)
    if not isinstance(include, bool):
        raise ProfileError("contacts.include_in_responses: нужно true или false")
    contacts = {k: v for k, v in contacts_table.items() if k != "include_in_responses"}
    if not all(isinstance(v, str) for v in contacts.values()):
        raise ProfileError("contacts: значения контактов — строки")
    contacts = {k: v.strip() for k, v in contacts.items() if v.strip()}

    forbidden = _str_list(raw, "forbidden_claims")
    for pattern in forbidden:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProfileError(f"forbidden_claims: неверное выражение {pattern!r}: {exc}") from None

    experience = raw.get("experience_years", 0)
    if isinstance(experience, bool) or not isinstance(experience, int | float) or not (
        0 <= experience <= 60
    ):
        raise ProfileError("profile: experience_years — число лет от 0 до 60, например 2.5")
    built = raw.get("built_bots_or_agents", False)
    if not isinstance(built, bool):
        raise ProfileError("profile: built_bots_or_agents — true или false")

    return Profile(
        experience_years=float(experience),
        built_bots_or_agents=built,
        name=name.strip(),
        confirmed_experience=_parse_facts(raw.get("confirmed_experience", [])),
        target_directions=_str_list(raw, "target_directions"),
        forbidden_claims=forbidden,
        contacts=contacts,
        include_contacts=include,
    )


def _parse_facts(raw: object) -> list[Fact]:
    if not isinstance(raw, list):
        raise ProfileError("confirmed_experience: нужен список таблиц [[confirmed_experience]]")
    facts: list[Fact] = []
    for i, item in enumerate(raw):
        where = f"confirmed_experience[{i}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: нужна таблица")
        for key in ("id", "text", "source"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ProfileError(
                    f"{where}.{key}: нужна непустая строка"
                    + (" — дословная строка резюме" if key == "source" else "")
                )
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ProfileError(f"{where}.tags: нужен список направлений")
        facts.append(
            Fact(id=item["id"], text=item["text"].strip(), tags=list(tags), source=item["source"])
        )
    return facts


def _str_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ProfileError(f"profile: {key} — нужен список непустых строк")
    return list(value)
