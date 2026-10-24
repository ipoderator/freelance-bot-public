"""Загрузка настроек: несекретное — из settings.toml, секреты — из .env.

Значения секретов никогда не попадают в repr/str/сообщения об ошибках:
в тексте ошибок упоминаются только имена переменных.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from freelance_bot.models import MODES, PLATFORMS, Mode, Platform

MIN_INTERVAL_SEC = 60
MIN_DIGEST_INTERVAL_MIN = 15  # чаще — уже не дайджест, а поток карточек
DEFAULT_LLM_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_INTERVALS: dict[str, int] = {"fl": 180, "kwork": 300}


class ConfigError(Exception):
    """Ошибка в настройках: текст понятен пользователю и не содержит секретов."""


@dataclass(frozen=True)
class Direction:
    keywords: list[str]
    weight: float = 1.0


@dataclass(frozen=True)
class Intersection:
    directions: tuple[str, str]
    bonus: int


@dataclass(frozen=True)
class SourceSettings:
    enabled: bool
    interval_sec: int
    urls: list[str]
    mode: Mode = "draft"
    daily_auto_limit: int = 0
    jitter_sec: int = 0
    # Только FL: присылать лишь проекты с бесплатным откликом (метка «для всех»).
    # Остальные отклики FL платные (docs/platforms.md, §1). На Kwork не действует.
    only_free_responses: bool = True


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None = field(repr=False)
    owner_id: int | None
    anthropic_api_key: str | None = field(repr=False)
    llm_model: str
    db_path: Path
    timezone: str
    max_age_hours: float
    min_score: int
    directions: dict[str, Direction]
    intersections: list[Intersection]
    exclusions: dict[str, list[str]]
    sources: dict[Platform, SourceSettings]
    kwork_connects_daily_limit: int = 0
    requirement_penalty: int = 15
    # Подтверждённый стаж разработки, лет: требование заказчика «опыт от N лет» больше него
    # считается невыполнимым (scoring.experience_limit)
    experience_years: float = 0.0
    offer_price_rub: int | None = None
    offer_days: int | None = None
    dry_run: bool = False
    # GPT вместо Claude: задан ключ — черновики и суть идут в OpenAI (openai_llm.py)
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = DEFAULT_OPENAI_MODEL
    # Groq (OpenAI-совместимый API, есть бесплатный тариф) важнее ключей OpenAI и Anthropic
    groq_api_key: str | None = field(default=None, repr=False)
    groq_model: str = DEFAULT_GROQ_MODEL
    # Дайджест: новые заказы копятся и приходят списком раз в interval_min, сетка от
    # start_hour:00 (11:00, 12:00, …); выключен — каждая находка сразу карточкой.
    digest_enabled: bool = False
    digest_start_hour: int = 11
    digest_interval_min: int = 60

    @property
    def has_llm_key(self) -> bool:
        """Задан ключ хоть одной нейросети — черновики и суть просят у неё (make_llm_client)."""
        return bool(self.groq_api_key or self.openai_api_key or self.anthropic_api_key)


def load_settings(path: Path = Path("settings.toml"), env_file: Path = Path(".env")) -> Settings:
    raw = _read_toml(Path(path))
    env = _read_env(Path(env_file))

    general = _table(raw, "general")
    storage = _table(raw, "storage")
    scoring = _table(raw, "scoring")
    offer = _table(raw, "offer")
    kwork = _table(raw, "kwork")
    digest = _table(raw, "digest")

    directions = _active_directions(
        _parse_directions(_table(raw, "directions")), general.get("directions", [])
    )
    return Settings(
        telegram_token=env.get("TELEGRAM_BOT_TOKEN"),
        owner_id=_parse_owner_id(env.get("TELEGRAM_OWNER_ID")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY"),
        llm_model=env.get("LLM_MODEL") or DEFAULT_LLM_MODEL,
        openai_api_key=env.get("OPENAI_API_KEY"),
        openai_model=env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        groq_api_key=env.get("GROQ_API_KEY"),
        groq_model=env.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
        db_path=Path(_str(storage, "db_path", "storage.db_path", "data/bot.sqlite3")),
        timezone=_str(general, "timezone", "general.timezone", "Europe/Moscow"),
        max_age_hours=_number(general, "max_age_hours", "general.max_age_hours", 6),
        min_score=_int(general, "min_score", "general.min_score", 40, max_value=100),
        directions=directions,
        intersections=_parse_intersections(
            raw.get("intersections", []), directions, _table(raw, "directions")
        ),
        exclusions=_parse_exclusions(_table(raw, "exclusions")),
        sources=_parse_sources(_table(raw, "sources")),
        kwork_connects_daily_limit=_int(
            kwork, "connects_daily_limit", "kwork.connects_daily_limit", 0
        ),
        requirement_penalty=_int(
            scoring, "requirement_penalty", "scoring.requirement_penalty", 15, max_value=100
        ),
        experience_years=_number(scoring, "experience_years", "scoring.experience_years", 0),
        offer_price_rub=_optional_positive_int(offer, "price_rub", "offer.price_rub"),
        offer_days=_optional_positive_int(offer, "days", "offer.days"),
        dry_run=_bool(general, "dry_run", "general.dry_run", False),
        digest_enabled=_bool(digest, "enabled", "digest.enabled", False),
        digest_start_hour=_int(digest, "start_hour", "digest.start_hour", 11, max_value=23),
        digest_interval_min=max(
            _int(digest, "interval_min", "digest.interval_min", 60, max_value=24 * 60),
            MIN_DIGEST_INTERVAL_MIN,
        ),
    )


# ── чтение файлов ────────────────────────────────────────────────────────────


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Файл настроек {path} не найден (нужен settings.toml)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Файл настроек {path} — ошибка синтаксиса TOML: {exc}") from None


def _read_env(env_file: Path) -> dict[str, str | None]:
    """Значения из .env; переменные окружения процесса имеют приоритет. Пустое → None."""
    file_values = dotenv_values(env_file) if env_file.is_file() else {}
    names = (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "ANTHROPIC_API_KEY", "LLM_MODEL",
        "OPENAI_API_KEY", "OPENAI_MODEL", "GROQ_API_KEY", "GROQ_MODEL",
    )  # fmt: skip
    result: dict[str, str | None] = {}
    for name in names:
        value = os.environ.get(name)
        if value is None:
            value = file_values.get(name)
        value = value.strip() if value else None
        result[name] = value or None
    return result


def _parse_owner_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigError(
            "TELEGRAM_OWNER_ID в .env должен быть числом (ваш user_id в Telegram)"
        ) from None


# ── разделы ──────────────────────────────────────────────────────────────────


def _parse_sources(raw: dict[str, Any]) -> dict[Platform, SourceSettings]:
    unknown = set(raw) - set(PLATFORMS)
    if unknown:
        raise ConfigError(
            f"sources: неизвестная площадка {', '.join(sorted(unknown))}; "
            f"допустимо: {', '.join(PLATFORMS)}"
        )
    sources: dict[Platform, SourceSettings] = {}
    for platform in PLATFORMS:
        prefix = f"sources.{platform}"
        if platform not in raw:
            sources[platform] = SourceSettings(
                enabled=False, interval_sec=DEFAULT_INTERVALS[platform], urls=[]
            )
            continue
        table = _table(raw, platform, prefix)
        mode = table.get("mode", "draft")
        if mode not in MODES:
            raise ConfigError(
                f"{prefix}.mode: неверный режим {mode!r}; допустимо: {', '.join(MODES)}"
            )
        urls = table.get("urls", [])
        if not isinstance(urls, list) or not all(isinstance(u, str) and u for u in urls):
            raise ConfigError(f"{prefix}.urls: нужен список адресов-строк")
        interval = _int(
            table, "interval_sec", f"{prefix}.interval_sec", DEFAULT_INTERVALS[platform]
        )
        only_free = True
        if platform == "fl":
            only_free = _bool(table, "only_free_responses", f"{prefix}.only_free_responses", True)
        elif "only_free_responses" in table:
            raise ConfigError(
                f"{prefix}.only_free_responses: настройка действует только для sources.fl"
            )
        sources[platform] = SourceSettings(
            enabled=_bool(table, "enabled", f"{prefix}.enabled", True),
            interval_sec=max(interval, MIN_INTERVAL_SEC),
            urls=list(urls),
            mode=mode,
            daily_auto_limit=_int(table, "daily_auto_limit", f"{prefix}.daily_auto_limit", 0),
            jitter_sec=_int(table, "jitter_sec", f"{prefix}.jitter_sec", 0),
            only_free_responses=only_free,
        )
    return sources


def _parse_directions(raw: dict[str, Any]) -> dict[str, Direction]:
    directions: dict[str, Direction] = {}
    for name in raw:
        table = _table(raw, name, f"directions.{name}")
        directions[name] = Direction(
            keywords=_str_list(table, "keywords", f"directions.{name}.keywords"),
            weight=_number(table, "weight", f"directions.{name}.weight", 1.0),
        )
    return directions


def _active_directions(directions: dict[str, Direction], active: Any) -> dict[str, Direction]:
    """general.directions — какие направления включены; пустой список — все."""
    if not isinstance(active, list) or not all(isinstance(d, str) for d in active):
        raise ConfigError("general.directions: нужен список названий направлений")
    unknown = [d for d in active if d not in directions]
    if unknown:
        raise ConfigError(
            f"general.directions: неизвестное направление {', '.join(unknown)}; "
            f"есть: {', '.join(directions) or '—'}"
        )
    if not active:
        return directions
    return {name: d for name, d in directions.items() if name in active}


def _parse_intersections(
    raw: Any, directions: dict[str, Direction], declared: dict[str, Any] | None = None
) -> list[Intersection]:
    """Бонусы пересечений; пара с выключенным (через general.directions) направлением
    пропускается, с несуществующим — ошибка."""
    if not isinstance(raw, list):
        raise ConfigError("intersections: нужен список таблиц [[intersections]]")
    result: list[Intersection] = []
    for i, item in enumerate(raw):
        where = f"intersections[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: нужна таблица с directions и bonus")
        pair = item.get("directions")
        if not (
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(d, str) for d in pair)
        ):
            raise ConfigError(f"{where}.directions: нужна пара названий направлений")
        if declared is not None and all(d in declared for d in pair) and not all(
            d in directions for d in pair
        ):
            continue  # направление выключено пользователем
        unknown = [d for d in pair if d not in directions]
        if unknown:
            raise ConfigError(
                f"{where}.directions: неизвестное направление {', '.join(unknown)}; "
                f"есть: {', '.join(directions) or '—'}"
            )
        result.append(
            Intersection(
                directions=(pair[0], pair[1]), bonus=_int(item, "bonus", f"{where}.bonus", 0)
            )
        )
    return result


def _parse_exclusions(raw: dict[str, Any]) -> dict[str, list[str]]:
    return {name: _str_list(raw, name, f"exclusions.{name}") for name in raw}


# ── примитивы с понятными ошибками ───────────────────────────────────────────


def _table(raw: dict[str, Any], key: str, where: str | None = None) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where or key}: ожидается раздел [{where or key}]")
    return value


def _int(
    table: dict[str, Any], key: str, where: str, default: int, *, max_value: int | None = None
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: нужно целое число, получено {value!r}")
    if value < 0 or (max_value is not None and value > max_value):
        bound = f"от 0 до {max_value}" if max_value is not None else "не меньше 0"
        raise ConfigError(f"{where}: значение {value} вне диапазона ({bound})")
    return value


def _number(table: dict[str, Any], key: str, where: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{where}: нужно неотрицательное число, получено {value!r}")
    return float(value)


def _bool(table: dict[str, Any], key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: нужно true или false, получено {value!r}")
    return value


def _str(table: dict[str, Any], key: str, where: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: нужна непустая строка")
    return value


def _str_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where}: нужен список непустых строк")
    return list(value)


def _optional_positive_int(table: dict[str, Any], key: str, where: str) -> int | None:
    value = table.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f'{where}: нужно целое число больше 0 или "" (пусто), получено {value!r}')
    return value
