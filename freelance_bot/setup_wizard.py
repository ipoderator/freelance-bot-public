"""Мастер первичной настройки своей копии бота: `python -m freelance_bot init`.

Спрашивает токен Telegram, user_id владельца, ключ LLM (необязательно), профиль (имя, стаж,
факты опыта, направления, свои ключевые слова) и параметры отбора, затем пишет `.env`,
`profile/profile.toml` и правит `settings.toml`. Всё остаётся на этом компьютере: в сеть
мастер ходит только в Telegram (проверить токен и узнать user_id).

Безопасность: секреты вводятся без эха и не печатаются; `.env` — права 600; прежние файлы
сохраняются в `*.bak`; ввод проверяется и экранируется (TOML-строки через json.dumps), а
итоговые настройки и профиль проверяются загрузчиками до записи.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from freelance_bot.config import ConfigError, load_settings
from freelance_bot.profile import ProfileError, load_profile

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
GROQ_KEY_RE = re.compile(r"^gsk_[A-Za-z0-9]{20,}$")
OPENAI_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{20,}$")
ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё -]{0,39}$")
KEYWORD_RE = re.compile(r"^[\w .+#/-]{2,40}$")
# В фактах опыта не место контактам: они уходят в отклики, а площадки за них наказывают
CONTACT_RE = re.compile(r"@|https?://|www\.|\+?\d[\d\s()-]{8,}\d")
ATTEMPTS = 3
CUSTOM = "custom"

DIRECTION_TITLES: dict[str, str] = {
    "backend": "Backend на Python: API, сервисы, интеграции",
    "telegram": "Telegram-боты",
    "ai_agents": "AI-агенты и интеграции с LLM",
    "automation": "Автоматизация бизнес-процессов",
    "parsing": "Парсинг и сбор данных",
    CUSTOM: "Свои ключевые слова",
}
DEFAULT_FORBIDDEN = [
    "(senior|сеньор|тимлид|team ?lead)",
    "(много|десятки|сотни)\\s+(успешных\\s+)?проектов",
    "(гарантирую|гарантия)\\s+(результат|100)",
]
ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "GROQ_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)  # fmt: skip


class Prompter(Protocol):
    def ask(self, question: str, default: str = "") -> str: ...

    def secret(self, question: str) -> str: ...

    def out(self, text: str) -> None: ...


class ConsolePrompter:
    def ask(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        answer = input(f"{question}{hint}: ").strip()
        return answer or default

    def secret(self, question: str) -> str:
        import getpass

        return getpass.getpass(f"{question} (ввод не отображается): ").strip()

    def out(self, text: str) -> None:
        print(text)


class SetupAborted(Exception):
    """Пользователь отказался или не смог ввести допустимое значение."""


@dataclass
class Answers:
    token: str = field(default="", repr=False)
    owner_id: int = 0
    groq_key: str = field(default="", repr=False)
    openai_key: str = field(default="", repr=False)
    anthropic_key: str = field(default="", repr=False)
    name: str = ""
    experience_years: float = 0.0
    built_bots_or_agents: bool = False
    facts: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    min_score: int = 25
    digest: bool = True
    digest_start_hour: int = 11
    kwork: bool = False


TokenCheck = Callable[[str], Awaitable[str | None]]  # → username бота или None
OwnerWait = Callable[[str], Awaitable[int | None]]  # → user_id написавшего /start или None


# ── ввод ─────────────────────────────────────────────────────────────────────


def _retry(p: Prompter, read: Callable[[], str], parse: Callable[[str], object], error: str):
    for _ in range(ATTEMPTS):
        raw = read()
        try:
            value = parse(raw)
        except ValueError:
            value = None
        if value is not None:
            return value
        p.out(f"  ✗ {error}")
    raise SetupAborted(f"не удалось получить значение: {error}")


def _yes(p: Prompter, question: str, default: bool) -> bool:
    def parse(raw: str) -> bool | None:
        raw = raw.strip().lower()
        if raw in ("y", "yes", "д", "да"):
            return True
        if raw in ("n", "no", "н", "нет"):
            return False
        return None

    return _retry(
        p, lambda: p.ask(f"{question} (да/нет)", "да" if default else "нет"), parse,
        "ответьте «да» или «нет»",
    )


def _int_in(lo: int, hi: int) -> Callable[[str], int | None]:
    def parse(raw: str) -> int | None:
        value = int(raw)
        return value if lo <= value <= hi else None

    return parse


def _years(raw: str) -> float | None:
    value = float(raw.replace(",", "."))
    return value if 0 <= value <= 60 else None


def _match(regex: re.Pattern[str]) -> Callable[[str], str | None]:
    return lambda raw: raw if regex.match(raw) else None


def _name(raw: str) -> str | None:
    raw = " ".join(raw.split())
    return raw if NAME_RE.match(raw) else None


def _fact(raw: str) -> str | None:
    raw = " ".join(raw.split())
    if not 10 <= len(raw) <= 300 or CONTACT_RE.search(raw):
        return None
    return raw


def _choose_directions(p: Prompter, available: list[str]) -> list[str]:
    options = [*available, CUSTOM]
    for number, key in enumerate(options, start=1):
        p.out(f"  {number}. {DIRECTION_TITLES.get(key, key)}")

    def parse(raw: str) -> list[str] | None:
        if not raw.strip():
            return list(available)
        picked = []
        for part in re.split(r"[,\s]+", raw.strip()):
            index = int(part) - 1
            if not 0 <= index < len(options):
                return None
            if options[index] not in picked:
                picked.append(options[index])
        return picked or None

    return _retry(
        p, lambda: p.ask("Номера через запятую (Enter — все готовые)"), parse,
        f"номера от 1 до {len(options)}, например 1,3",
    )


def _keywords(raw: str) -> list[str] | None:
    words = [" ".join(w.split()).lower() for w in raw.split(",") if w.strip()]
    if not words or len(words) > 40 or not all(KEYWORD_RE.match(w) for w in words):
        return None
    return list(dict.fromkeys(words))


async def ask_answers(
    p: Prompter, available: list[str], check_token: TokenCheck, wait_owner: OwnerWait
) -> Answers:
    a = Answers()
    p.out("\n1/4 · Telegram")
    p.out("  Создайте бота: @BotFather → /newbot → имя и username (оканчивается на bot).")
    while True:
        a.token = _retry(p, lambda: p.secret("Токен бота"), _match(TOKEN_RE),
                         "токен вида 123456789:AA… — скопируйте его из @BotFather целиком")
        username = await check_token(a.token)
        if username:
            p.out(f"  ✓ Токен принят: бот @{username}")
            break
        p.out("  ✗ Telegram не принял токен (или нет сети).")
        if not _yes(p, "Ввести токен заново?", True):
            break
    owner = None
    if _yes(p, "Узнать ваш Telegram ID автоматически? Нужно будет написать боту /start", True):
        p.out("  Откройте своего бота в Telegram и отправьте /start…")
        owner = await wait_owner(a.token)
        p.out(f"  ✓ Ваш Telegram ID: {owner}" if owner else "  ✗ Не дождался /start.")
    a.owner_id = owner or _retry(
        p, lambda: p.ask("Ваш числовой Telegram ID (подскажет @userinfobot)"),
        _int_in(1, 10**15), "только цифры",
    )

    p.out("\n2/4 · Нейросеть для откликов (необязательно)")
    p.out("  1. OpenAI (GPT)   2. Anthropic (Claude)   3. Без нейросети — встроенный шаблон")
    p.out("  4. Groq — есть бесплатный тариф, ключ на console.groq.com/keys")
    llm = _retry(p, lambda: p.ask("Вариант", "3"), _int_in(1, 4), "1, 2, 3 или 4")
    if llm == 4:
        a.groq_key = _retry(p, lambda: p.secret("Ключ Groq"), _match(GROQ_KEY_RE),
                            "ключ начинается с gsk_")
    elif llm == 1:
        a.openai_key = _retry(p, lambda: p.secret("Ключ OpenAI"), _match(OPENAI_KEY_RE),
                              "ключ начинается с sk-")
    elif llm == 2:
        a.anthropic_key = _retry(p, lambda: p.secret("Ключ Anthropic"),
                                 _match(ANTHROPIC_KEY_RE), "ключ начинается с sk-ant-")

    p.out("\n3/4 · Профиль — из него пишутся отклики, выдумывать опыт бот не будет")
    a.name = _retry(p, lambda: p.ask("Имя для подписи (только имя)"), _name,
                    "только буквы, пробел и дефис, до 40 символов")
    a.experience_years = _retry(p, lambda: p.ask("Стаж разработки, лет (например 2.5)", "0"),
                                _years, "число от 0 до 60")
    a.built_bots_or_agents = _yes(p, "Уже делали Telegram-ботов или AI-агентов?", False)
    p.out("  Факты опыта для откликов — по одному в строке, пустая строка — конец.")
    p.out("  Только правда и без контактов: «2 года backend на Python в компании N».")
    while len(a.facts) < 20:
        raw = p.ask(f"  Факт {len(a.facts) + 1}")
        if not raw:
            if a.facts:
                break
            p.out("  ✗ Нужен хотя бы один факт")
            continue
        fact = _fact(raw)
        if fact is None:
            p.out("  ✗ 10–300 символов, без ссылок, @ников и телефонов")
            continue
        a.facts.append(fact)
    p.out("  Направления заказов:")
    a.directions = _choose_directions(p, available)
    if CUSTOM in a.directions:
        a.keywords = _retry(
            p, lambda: p.ask("Свои ключевые слова через запятую (например: vue, nuxt, верстка)"),
            _keywords, "слова 2–40 символов через запятую, не больше 40 слов",
        )

    p.out("\n4/4 · Отбор и уведомления")
    a.min_score = _retry(p, lambda: p.ask("Порог релевантности 0–100 (меньше — больше заказов)",
                                          "25"), _int_in(0, 100), "число от 0 до 100")
    a.digest = _yes(p, "Присылать новые заказы списком раз в час?", True)
    if a.digest:
        a.digest_start_hour = _retry(p, lambda: p.ask("С какого часа (0–23)", "11"),
                                     _int_in(0, 23), "час от 0 до 23")
    p.out("  ⚠ Соглашение Kwork запрещает автоматический сбор: аккаунт могут заблокировать.")
    a.kwork = _yes(p, "Включить Kwork на свой риск?", False)
    return a


# ── запись ───────────────────────────────────────────────────────────────────


def _toml(value: object) -> str:
    """TOML-литерал: строки — JSON-экранирование (валидно для базовых строк TOML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return repr(value)


_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _sections(lines: list[str]) -> list[tuple[str, int]]:
    return [(m.group(1), i) for i, line in enumerate(lines) if (m := _HEADER_RE.match(line))]


def set_toml_value(text: str, section: str, key: str, value: object) -> str:
    """Задать однострочный `key = value` в `[section]`, сохранив комментарии и остальное."""
    lines = text.splitlines(keepends=True)
    heads = _sections(lines)
    start = next((i for name, i in heads if name == section), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {_toml(value)}\n"
    end = next((i for _, i in heads if i > start), len(lines))
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = f"{key} = {_toml(value)}\n"
            return "".join(lines)
    lines.insert(start + 1, f"{key} = {_toml(value)}\n")
    return "".join(lines)


def set_custom_direction(text: str, keywords: list[str]) -> str:
    """Заменить блок `[directions.custom]` (или убрать, если слов нет)."""
    lines = text.splitlines(keepends=True)
    heads = _sections(lines)
    start = next((i for name, i in heads if name == f"directions.{CUSTOM}"), None)
    if start is not None:
        end = next((i for _, i in heads if i > start), len(lines))
        del lines[start:end]
    text = "".join(lines).rstrip("\n") + "\n"
    if keywords:
        text += (
            f"\n[directions.{CUSTOM}]\n# Свои ключевые слова из мастера настройки\n"
            f"weight = 1.0\nkeywords = {_toml(keywords)}\n"
        )
    return text


def render_settings(template: str, a: Answers) -> str:
    text = set_toml_value(template, "general", "min_score", a.min_score)
    text = set_toml_value(text, "general", "directions", a.directions)
    text = set_toml_value(text, "scoring", "experience_years", a.experience_years)
    text = set_toml_value(text, "digest", "enabled", a.digest)
    text = set_toml_value(text, "digest", "start_hour", a.digest_start_hour)
    text = set_toml_value(text, "sources.kwork", "enabled", a.kwork)
    return set_custom_direction(text, a.keywords if CUSTOM in a.directions else [])


def _fact_tags(fact: str, settings_text: str, directions: list[str]) -> list[str]:
    """Направления, ключевые слова которых встречаются в факте (для подбора к заказу)."""
    from freelance_bot.textmatch import find_terms

    raw = tomllib.loads(settings_text).get("directions", {})
    tags = [d for d in directions if find_terms(fact, raw.get(d, {}).get("keywords", []))]
    return tags or [d for d in directions if d != CUSTOM][:1] or list(directions[:1])


def render_profile(a: Answers, settings_text: str) -> str:
    targets = [DIRECTION_TITLES.get(d, d) for d in a.directions if d != CUSTOM]
    if a.keywords:
        targets.append("Заказы по темам: " + ", ".join(a.keywords[:8]))
    parts = [
        "# Профиль для черновиков откликов (создан мастером: python -m freelance_bot init).",
        "# confirmed_experience — только правда: бот берёт факты отсюда и ничего не выдумывает.",
        "",
        f"name = {_toml(a.name)}",
        f"experience_years = {a.experience_years:g}",
        f"built_bots_or_agents = {_toml(a.built_bots_or_agents)}",
        f"target_directions = {_toml(targets)}",
        f"forbidden_claims = {_toml(DEFAULT_FORBIDDEN)}",
        "",
        "[contacts]",
        "# Контакты в откликах выключены: площадки за них наказывают.",
        "include_in_responses = false",
    ]
    for number, fact in enumerate(a.facts, start=1):
        parts += [
            "",
            "[[confirmed_experience]]",
            f'id = "fact_{number}"',
            f"text = {_toml(fact)}",
            f"tags = {_toml(_fact_tags(fact, settings_text, a.directions))}",
            f"source = {_toml(fact)}",
        ]
    return "\n".join(parts) + "\n"


def render_env(existing: str, a: Answers) -> str:
    """Свои ключи — заменить или дописать; остальные строки (модели, комментарии) — сохранить."""
    values = {
        "TELEGRAM_BOT_TOKEN": a.token,
        "TELEGRAM_OWNER_ID": str(a.owner_id),
        "GROQ_API_KEY": a.groq_key,
        "OPENAI_API_KEY": a.openai_key,
        "ANTHROPIC_API_KEY": a.anthropic_key,
    }
    return set_env_values(existing, values)


def set_env_values(existing: str, values: dict[str, str]) -> str:
    """Заменить или дописать ключи `values`; остальные строки — как были."""
    lines, seen = [], set()
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip()
        if key in values and "=" in line and not line.lstrip().startswith("#"):
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(lines).rstrip("\n") + "\n"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    if path.name == ".env":
        os.chmod(backup, 0o600)
    return backup


def update_env_file(path: Path, values: dict[str, str]) -> Path | None:
    """Записать ключи в .env (права 600, прежний — .env.bak). Возвращает путь копии."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    backup = _backup(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(set_env_values(existing, values))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return backup


def write_files(root: Path, a: Answers, out: Callable[[str], None]) -> None:
    """Проверить, что получившиеся настройки и профиль загружаются, и только потом записать."""
    settings_path = root / "settings.toml"
    profile_path = root / "profile" / "profile.toml"
    env_path = root / ".env"
    env_template = root / ".env.example"

    settings_text = render_settings(settings_path.read_text(encoding="utf-8"), a)
    profile_text = render_profile(a, settings_text)
    existing_env = (env_path if env_path.exists() else env_template)
    env_text = render_env(existing_env.read_text(encoding="utf-8") if existing_env.exists()
                          else "", a)

    with tempfile.TemporaryDirectory() as tmp_dir:  # каталог 700: в нём копия секретов
        check = Path(tmp_dir)
        _validate(check, settings_text, profile_text, env_text)

    for path, text, mode in (
        (settings_path, settings_text, None),
        (profile_path, profile_text, None),
        (env_path, env_text, 0o600),
    ):
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # секреты: файл сразу создаётся с правами 600, без промежуточных 644
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)  # если .tmp остался от прерванного запуска с другими правами
        tmp.replace(path)
        saved = f" (прежний — {backup.name})" if backup else ""
        out(f"  ✓ {path.relative_to(root)}{saved}")


def _validate(check: Path, settings_text: str, profile_text: str, env_text: str) -> None:
    try:
        (check / "settings.toml").write_text(settings_text, encoding="utf-8")
        (check / "profile.toml").write_text(profile_text, encoding="utf-8")
        env = check / ".env"
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(env_text)
        load_settings(check / "settings.toml", env)
        load_profile(check / "profile.toml")
    except (ConfigError, ProfileError) as exc:
        raise SetupAborted(f"получились неверные настройки: {exc}") from exc


async def run_wizard(
    p: Prompter, root: Path, check_token: TokenCheck, wait_owner: OwnerWait
) -> int:
    p.out("Настройка своей копии бота. Данные остаются на этом компьютере;")
    p.out("в сеть мастер обращается только к Telegram — проверить токен и узнать ваш ID.")
    settings_path = root / "settings.toml"
    if not settings_path.exists():
        p.out(f"Не найден {settings_path}: запустите из папки проекта.")
        return 1
    if (root / ".env").exists() and not _yes(
        p, "Настройки уже есть. Перенастроить? Прежние файлы сохранятся в *.bak", False
    ):
        p.out("Ничего не изменено.")
        return 0
    available = [
        d for d in tomllib.loads(settings_path.read_text(encoding="utf-8")).get("directions", {})
        if d != CUSTOM
    ]
    try:
        answers = await ask_answers(p, available, check_token, wait_owner)
        p.out("\nЗаписываю:")
        write_files(root, answers, p.out)
    except SetupAborted as exc:
        p.out(f"Настройка прервана: {exc}. Ничего не записано.")
        return 1
    p.out("\nГотово. Запуск: .venv/bin/python -m freelance_bot run")
    p.out("На сервере: docker compose up -d --build (см. README, «Своя копия бота»).")
    return 0
