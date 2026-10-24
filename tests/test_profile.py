"""Профиль: факты из резюме с дословным источником, направления без утверждений об опыте."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from freelance_bot.profile import Profile, ProfileError, load_profile

# Признаки личных данных — общие шаблоны, без самих данных пользователя.
PERSONAL_PATTERNS = {
    "телефон": r"\+7|\(\d{3}\)\s*\d{3}|\b8\s*\(?\d{3}",
    "email": r"[\w.+-]+@[\w-]+\.\w+",
    "telegram-ник": r"(?<![\w.])@[A-Za-z0-9_]{3,}|telegram:",
    "дата рождения": r"родил|\b19\d{2}\b",
    "возраст": r"\b\d{2}\s+(лет|год)[а]?,",
    "город/адрес": r"Проживает|Гражданство|Мужчина|Женщина",
    "отчество": r"\b[А-ЯЁ][а-яё]+(ович|евич|овна|евна|ична)\b",
}


# Свой профиль и вымышленный пример (на нём идут остальные тесты, он же — профиль публичной копии)
PROFILES = [
    ("profile.toml", "resume_facts.txt"),
    ("profile.example.toml", "resume_facts.example.txt"),
]


@pytest.fixture(params=PROFILES, ids=[p for p, _ in PROFILES])
def files(request: pytest.FixtureRequest) -> tuple[str, str]:
    return request.param


@pytest.fixture
def profile(repo_root: Path, files: tuple[str, str]) -> Profile:
    return load_profile(repo_root / "profile" / files[0])


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_fact_source_is_a_line_of_resume_facts(
    profile: Profile, repo_root: Path, files: tuple[str, str]
) -> None:
    resume_lines = set((repo_root / "profile" / files[1]).read_text(encoding="utf-8").splitlines())
    assert len(profile.confirmed_experience) >= 5
    for fact in profile.confirmed_experience:
        assert fact.source, f"у факта {fact.id} нет source"
        assert fact.source in resume_lines, f"source факта {fact.id} не найден в резюме"


def test_profile_signature_and_contacts_are_private_by_default(profile: Profile) -> None:
    assert profile.name
    assert profile.contacts == {}
    assert profile.include_contacts is False


def test_target_directions_cover_wanted_work_without_experience_claims(profile: Profile) -> None:
    joined = " ".join(profile.target_directions).lower()
    for direction in ("telegram", "ai-агент", "автоматизац", "парсинг"):
        assert direction in joined
    assert not re.search(r"опыт|делал|сделал|разработал|выполнил|\bлет\b", joined)


def test_confirmed_experience_has_no_bot_or_agent_projects_and_no_five_years(
    profile: Profile,
) -> None:
    for fact in profile.confirmed_experience:
        text = fact.text.lower()
        assert not re.search(r"telegram|телеграм|\bбот|агент", text), fact.id
        assert not re.search(r"\b(5|пять)\s+лет", text), fact.id


@pytest.mark.parametrize("kind", sorted(PERSONAL_PATTERNS))
def test_resume_facts_and_profile_have_no_personal_data(
    repo_root: Path, kind: str, files: tuple[str, str]
) -> None:
    for name in files:
        text = (repo_root / "profile" / name).read_text(encoding="utf-8")
        assert not re.search(PERSONAL_PATTERNS[kind], text), f"{kind} в {name}"


def test_fact_without_source_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        'name = "Иван"\ntarget_directions = []\n'
        '[[confirmed_experience]]\nid = "x"\ntext = "делал API"\ntags = ["backend"]\n',
    )
    with pytest.raises(ProfileError, match="source"):
        load_profile(path)


def test_contacts_enabled_explicitly_are_loaded(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        'name = "Иван"\ntarget_directions = []\n'
        '[contacts]\ninclude_in_responses = true\ntelegram = "@example_nick"\n',
    )
    loaded = load_profile(path)
    assert loaded.include_contacts is True
    assert loaded.contacts == {"telegram": "@example_nick"}


def test_missing_profile_file_gives_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="не найден"):
        load_profile(tmp_path / "nope.toml")


def test_bad_forbidden_claim_regex_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, 'name = "Иван"\ntarget_directions = []\nforbidden_claims = ["("]\n')
    with pytest.raises(ProfileError, match="forbidden_claims"):
        load_profile(path)
