"""Резюме нового пользователя (анкета публичного бота, ADR 0011): текст из файла или
ссылки → профиль для поиска (направления, ключевые слова, стаж, факты опыта с цитатами).

Нейросеть только предлагает: каждое ключевое слово должно встретиться в резюме, каждый
факт — опираться на дословную строку из него (как в ADR 0005), иначе он отбрасывается.
Нет ключа или ответ не разобран — встроенный разбор по ключевым словам из settings.toml.

Ссылки — только Google Docs/Диск и Яндекс Диск: адрес присылает посторонний человек, а
запрос уходит с сервера, поэтому произвольные адреса не открываются (SSRF). Резюме hh.ru
по ссылке не скачивается — условия hh.ru запрещают автоматический сбор (ADR 0009).
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from defusedxml import ElementTree

from freelance_bot.config import Settings
from freelance_bot.scoring import DIRECTION_LABELS
from freelance_bot.textmatch import find_terms

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024  # Telegram отдаёт ботам файлы до 20 МБ; резюме меньше
MAX_DOCX_XML_BYTES = 5 * 1024 * 1024  # распакованный document.xml: защита от zip-бомбы
MAX_PDF_PAGES = 20
MIN_TEXT_CHARS = 200  # меньше — скан без текстового слоя или пустой файл
MAX_TEXT_CHARS = 60_000
# Резюме для нейросети: с промптом и ответом запрос влезает в 8K токенов в минуту Groq
RESUME_MAX_CHARS = 5000
RESUME_MAX_TOKENS = 3000
MAX_KEYWORDS = 20
MAX_FACTS = 12
MAX_FACT_CHARS = 200
LINK_TIMEOUT_SEC = 30.0
MAX_REDIRECTS = 5
# Адреса, куда разрешено ходить по ссылке пользователя (и куда разрешены переадресации)
ALLOWED_HOST_SUFFIXES = ("google.com", "googleusercontent.com", "yandex.ru", "yandex.net",
                         "yadi.sk")  # fmt: skip
YANDEX_PUBLIC_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
LINK_USER_AGENT = "freelance-bot/1.0 (resume download)"

FORMATS_HINT = "PDF, DOCX или TXT"
HH_HINT = (
    "Резюме с hh.ru по ссылке не открываю: условия hh.ru запрещают автоматический сбор. "
    "Скачайте его файлом: hh.ru → «Мои резюме» → резюме → «Скачать» → PDF — и пришлите сюда."
)
YANDEX_FAILED = "Яндекс Диск не отдал файл. Проверьте доступ по ссылке."
LINK_HINT = (
    "По ссылке открываю только Google Docs, Google Диск и Яндекс Диск (доступ «всем, у кого "
    f"есть ссылка»). Другое — пришлите файлом: {FORMATS_HINT}."
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_GDOC_RE = re.compile(r"^/document/d/([\w-]+)")
_GDRIVE_RE = re.compile(r"^/file/d/([\w-]+)")
_YEARS_RE = re.compile(
    r"(?:опыт\s+работы|work\s+experience)\D{0,15}?(\d{1,2})\s*(?:год\w*|лет|years?)"
    r"(?:\s*(\d{1,2})\s*(?:месяц\w*|months?))?",
    re.IGNORECASE,
)
_MONTHS_ONLY_RE = re.compile(
    r"(?:опыт\s+работы|work\s+experience)\D{0,15}?(\d{1,2})\s*(?:месяц\w*|months?)",
    re.IGNORECASE,
)
# Период места работы: «Ноябрь 2024 — Август 2026», «Июль 2021 —\nпо настоящее время»,
# «11.2024 – 08.2026», «Nov 2024 - present». Год без месяца (учёба «2016 — 2020») — не период.
_MONTH_WORD = (
    r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-zа-яё]*\.?"
)
_DATE = rf"(?:({_MONTH_WORD})\s+(\d{{4}})|(\d{{1,2}})[./](\d{{4}}))"
_PRESENT = (
    r"(?:по\s+)?(?:настоящее\s+время|наст\.?\s*вр(?:емя|\.)?|н\.\s*в\.|сейчас"
    r"|present|now|current)"
)
_PERIOD_RE = re.compile(rf"{_DATE}\s*[—–-]{{1,2}}\s*(?:{_DATE}|({_PRESENT}))", re.IGNORECASE)
_MONTH_PREFIXES = (
    ("январ", "jan"), ("феврал", "feb"), ("март", "mar"), ("апрел", "apr"), ("ма", "may"),
    ("июн", "jun"), ("июл", "jul"), ("август", "aug"), ("сентябр", "sep"), ("октябр", "oct"),
    ("ноябр", "nov"), ("декабр", "dec"),
)  # fmt: skip
# Заголовки разделов: период после «Образование» — учёба, а не работа
_SECTION_RE = re.compile(
    r"^\s*(опыт работы|work experience|experience|образование|education|"
    r"повышение квалификации|курсы|courses|ключевые навыки|навыки|skills|обо мне|about me|"
    r"дополнительная информация)\b",
    re.IGNORECASE | re.MULTILINE,
)
_WORK_SECTIONS = ("опыт работы", "work experience", "experience")
# Место работы по профессии: бот ищет заказы на разработку (settings.toml directions)
IT_ROLE_TERMS = (
    "разработчик", "разработка программного", "программист", "developer", "programmer",
    "software", "backend", "back-end", "бэкенд", "frontend", "front-end", "фронтенд",
    "fullstack", "full-stack", "devops", "python", "тестировщик", "qa engineer",
    "qa-инженер", "data scientist", "data engineer", "ml engineer", "ml-инженер",
    "информационные технологии",
)  # fmt: skip
_BOT_WORDS = ("бот", "bot", "telegram", "телеграм", "агент", "agent", "llm")
_LIST_MARK_RE = re.compile(r"^[\s\-–—•·*●▪]+")


class ResumeError(Exception):
    """Резюме не прочитано: текст понятен пользователю."""


@dataclass(frozen=True)
class ResumeFact:
    text: str  # как факт звучит в отклике
    source: str  # дословная строка резюме


@dataclass(frozen=True)
class ResumeProfile:
    directions: list[str]  # id направлений из settings.directions
    keywords: list[str]  # слова из резюме для поиска (стек, предметная область)
    experience_years: float
    built_bots_or_agents: bool
    facts: list[ResumeFact] = field(default_factory=list)
    generator: str = "builtin"  # "llm" | "builtin"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResumeProfile:
        return cls(
            directions=list(raw.get("directions", [])),
            keywords=list(raw.get("keywords", [])),
            experience_years=float(raw.get("experience_years", 0)),
            built_bots_or_agents=bool(raw.get("built_bots_or_agents", False)),
            facts=[ResumeFact(**f) for f in raw.get("facts", [])],
            generator=raw.get("generator", "builtin"),
        )


# ── текст из файла ───────────────────────────────────────────────────────────


def extract_text(data: bytes, filename: str = "") -> str:
    """Текст резюме из PDF, DOCX или TXT; формат — по расширению, без него — по сигнатуре."""
    if len(data) > MAX_FILE_BYTES:
        raise ResumeError(f"Файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ.")
    ext = filename.lower().rpartition(".")[2] if "." in filename else ""
    if ext in ("doc", "rtf", "odt", "pages"):
        raise ResumeError(
            f"Формат .{ext} не читаю. Сохраните резюме как {FORMATS_HINT} и пришлите снова."
        )
    if ext == "pdf" or (not ext and data.startswith(b"%PDF")):
        text = _pdf_text(data)
    elif ext == "docx" or (not ext and data.startswith(b"PK")):
        text = _docx_text(data)
    elif ext in ("txt", "md", ""):
        text = _plain_text(data)
    else:
        raise ResumeError(f"Не знаю формат .{ext}. Пришлите {FORMATS_HINT}.")
    text = normalize(text)
    if len(text) < MIN_TEXT_CHARS:
        raise ResumeError(
            "В файле почти нет текста — возможно, это скан или картинка. "
            f"Пришлите {FORMATS_HINT} с текстом или вставьте резюме сообщением."
        )
    return text[:MAX_TEXT_CHARS]


def normalize(text: str) -> str:
    """Строки без лишних пробелов, пустые строки схлопнуты."""
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeError("PDF защищён паролем. Снимите пароль или пришлите DOCX.")
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join(page.extract_text() or "" for page in pages)
    except ResumeError:
        raise
    except Exception as exc:  # файл от постороннего: битый PDF роняет pypdf чем угодно
        log.info("PDF не прочитан: %s", type(exc).__name__)
        raise ResumeError("Не получилось открыть PDF. Пришлите DOCX или TXT.") from None


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ResumeError("DOCX слишком большой. Пришлите PDF или TXT.")
            root = ElementTree.fromstring(archive.read(info))
    except ResumeError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        log.info("DOCX не прочитан: %s", type(exc).__name__)
        raise ResumeError("Не получилось открыть DOCX. Пришлите PDF или TXT.") from None
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{_W}t" and el.text:
                parts.append(el.text)
            elif el.tag in (f"{_W}tab", f"{_W}br"):
                parts.append(" ")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _plain_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ResumeError(f"Не получилось прочитать текст. Пришлите {FORMATS_HINT}.")


# ── ссылки ───────────────────────────────────────────────────────────────────


def find_link(text: str) -> str | None:
    match = _URL_RE.search(text)
    return match.group(0).rstrip(").,;»\"'") if match else None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _allowed(url: str) -> bool:
    host = _host(url)
    return urlsplit(url).scheme == "https" and any(
        host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES
    )


def download_url(link: str) -> tuple[str, str]:
    """Адрес для скачивания и имя файла (по нему выбирается формат); иначе ResumeError."""
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    if host == "hh.ru" or host.endswith(".hh.ru") or "headhunter" in host:
        raise ResumeError(HH_HINT)
    if host == "docs.google.com" and (m := _GDOC_RE.match(parts.path)):
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt", "resume.txt"
    if host == "drive.google.com":
        m = _GDRIVE_RE.match(parts.path)
        file_id = m.group(1) if m else (parse_qs(parts.query).get("id") or [""])[0]
        if re.fullmatch(r"[\w-]+", file_id):
            return f"https://drive.google.com/uc?export=download&id={file_id}", ""
    if host in ("disk.yandex.ru", "yadi.sk", "disk.yandex.com") and parts.path.startswith(
        ("/d/", "/i/")
    ):
        return "yandex:" + link, ""
    raise ResumeError(LINK_HINT)


async def fetch_link(link: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Текст резюме по ссылке пользователя."""
    url, filename = download_url(link)
    async with httpx.AsyncClient(
        timeout=LINK_TIMEOUT_SEC, transport=transport, headers={"User-Agent": LINK_USER_AGENT}
    ) as client:
        if url.startswith("yandex:"):
            meta = await _get(client, YANDEX_PUBLIC_API, params={"public_key": url[7:]})
            try:
                url = json.loads(meta)["href"]
            except (ValueError, KeyError, TypeError):
                raise ResumeError(YANDEX_FAILED) from None
        data = await _get(client, url)
    if data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        raise ResumeError(
            "По ссылке открылась страница, а не файл. Откройте доступ «всем, у кого есть "
            "ссылка» или пришлите резюме файлом."
        )
    return extract_text(data, filename)


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> bytes:
    """GET с переадресациями только на разрешённые адреса и потолком размера."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url):
            raise ResumeError(LINK_HINT)
        try:
            async with client.stream("GET", url, params=params) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers.get("location", "")))
                    params = None
                    continue
                if response.status_code in (401, 403, 404):
                    raise ResumeError(
                        "Файл по ссылке недоступен. Откройте доступ «всем, у кого есть ссылка»."
                    )
                if response.status_code >= 400:
                    raise ResumeError("Сервис с файлом не ответил. Попробуйте позже или файлом.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ResumeError(f"Файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TransportError as exc:
            log.info("резюме по ссылке не скачано: %s", type(exc).__name__)
            raise ResumeError("Не получилось скачать файл. Попробуйте позже или файлом.") from None
    raise ResumeError("Слишком много переадресаций. Пришлите резюме файлом.")


# ── разбор ──────────────────────────────────────────────────────────────────


def builtin_profile(text: str, settings: Settings) -> ResumeProfile:
    """Без нейросети: направления и слова — по ключевым словам settings.toml, стаж — по
    строке «Опыт работы — N лет M месяцев» (вид резюме hh.ru)."""
    hits = {name: find_terms(text, d.keywords) for name, d in settings.directions.items()}
    ranked = sorted(hits, key=lambda name: len(hits[name]), reverse=True)
    directions = [name for name in ranked if len(hits[name]) >= 2]
    if not directions and ranked and hits[ranked[0]]:
        directions = [ranked[0]]
    keywords = _words_in_text(text, [t for name in ranked for t in hits[name]])[:MAX_KEYWORDS]
    return ResumeProfile(
        directions=directions,
        keywords=keywords,
        experience_years=experience_years(text),
        built_bots_or_agents=False,  # без подтверждения — как в профиле владельца: нельзя
    )


def _words_in_text(text: str, terms: list[str]) -> list[str]:
    """Слова резюме, найденные по терминам настроек: «автоматизац» → «автоматизацию»,
    «postgres» → «PostgreSQL». Обрывки фраз («интеграции с») пропускаются; без повторов."""
    words: dict[str, str] = {}
    for term in terms:
        parts = term.split()
        if len(parts) > 1 and len(parts[-1]) <= 2:
            continue
        body = r"\s+".join(re.escape(w) for w in parts)
        tail = r"[\w+#]*" if len(parts[-1]) > 3 else r"(?!\w)"
        if m := re.search(rf"(?<!\w){body}{tail}", text, re.IGNORECASE):
            words.setdefault(m.group(0).casefold(), " ".join(m.group(0).split()))
    return list(words.values())


@dataclass(frozen=True)
class Job:
    start: int  # номер месяца: год * 12 + месяц - 1
    end: int  # включительно: «Ноябрь 2024 — Август 2026» = 22 месяца, как пишет hh.ru
    text: str  # период и всё до следующего места работы или раздела


def _month(word: str | None, year: str | None, number: str | None, num_year: str | None
           ) -> int | None:  # fmt: skip
    if word and year:
        low = word.casefold()
        month = next(
            (i for i, prefixes in enumerate(_MONTH_PREFIXES, 1) if low.startswith(prefixes)),
            None,
        )
        return None if month is None else int(year) * 12 + month - 1
    if number and num_year and 1 <= int(number) <= 12:
        return int(num_year) * 12 + int(number) - 1
    return None


def work_periods(text: str, today: date) -> list[Job]:
    """Места работы с периодами; периоды в разделах учёбы и курсов пропускаются."""
    now = today.year * 12 + today.month - 1
    sections = [(m.start(), m.group(1).casefold()) for m in _SECTION_RE.finditer(text)]
    matches = list(_PERIOD_RE.finditer(text))
    jobs = []
    for i, m in enumerate(matches):
        section = next((name for pos, name in reversed(sections) if pos <= m.start()), None)
        if section is not None and section not in _WORK_SECTIONS:
            continue
        start = _month(*m.group(1, 2, 3, 4))
        end = now if m.group(9) else _month(*m.group(5, 6, 7, 8))
        if start is None or end is None or not start <= end or start > now:
            continue
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        tail = text[m.end() : stop]
        if header := _SECTION_RE.search(tail):
            tail = tail[: header.start()]
        jobs.append(Job(start, min(end, now), m.group(0) + tail))
    return jobs


def profession_years(text: str, today: date | None = None) -> float | None:
    """Стаж по местам работы в IT без пересечений; мест работы с периодами нет — None.
    Общая строка hh.ru «Опыт работы — 5 лет 1 месяц» считает и работу не по профессии."""
    jobs = work_periods(text, today or date.today())
    if not jobs:
        return None
    months: set[int] = set()
    for job in jobs:
        if find_terms(job.text, IT_ROLE_TERMS):
            months.update(range(job.start, job.end + 1))
    return round(len(months) / 12, 1)


def experience_years(text: str, today: date | None = None) -> float:
    """Стаж в профессии: по местам работы, а если их не разобрать — по общей строке."""
    years = profession_years(text, today)
    if years is not None:
        return years
    if m := _YEARS_RE.search(text):
        return round(int(m.group(1)) + int(m.group(2) or 0) / 12, 1)
    if m := _MONTHS_ONLY_RE.search(text):
        return round(int(m.group(1)) / 12, 1)
    return 0.0


_SYSTEM_PROMPT = """Ты разбираешь резюме фрилансера, чтобы искать ему заказы.
Ответь ТОЛЬКО объектом JSON без пояснений:
{"directions": [...], "keywords": [...], "experience_years": 0, "built_bots_or_agents": false,
 "facts": [{"text": "...", "source": "..."}]}
- directions — id из списка направлений, где у человека есть опыт или явное желание брать
  заказы; только id из списка.
- keywords — до 20 слов и терминов ДОСЛОВНО из резюме: языки, фреймворки, сервисы,
  предметные области (например "FastAPI", "PostgreSQL", "парсинг").
- experience_years — стаж работы по IT-специальности (разработка), лет: сложи периоды мест
  работы, где человек работал разработчиком (1 год 10 месяцев = 1.8). Работу не по
  профессии и общую строку «Опыт работы» не считай; нет данных — 0.
- built_bots_or_agents — true, только если в резюме прямо сказано, что человек делал
  Telegram-ботов или AI-агентов.
- facts — до 12 фактов опыта для откликов заказчикам: text — короткая фраза от первого лица
  в прошедшем времени («писал сервисы на FastAPI»), source — ДОСЛОВНАЯ строка резюме, на
  которой факт основан. Не придумывай ничего, чего нет в резюме."""


async def analyze(text: str, settings: Settings, client: Any = None) -> ResumeProfile:
    """Профиль из резюме: нейросетью (если есть ключ) с проверкой, иначе встроенный разбор."""
    builtin = builtin_profile(text, settings)
    if client is None and settings.has_llm_key:
        from freelance_bot.drafting import LlmUnavailable, make_llm_client

        try:
            client = make_llm_client(settings)
        except LlmUnavailable as exc:
            log.warning("разбор резюме без нейросети: %s", exc)
    if client is None:
        return builtin
    try:
        raw = await _ask(client, text, settings)
    except Exception as exc:  # любая ошибка API → встроенный разбор
        from freelance_bot.drafting import llm_error

        log.warning("резюме: нейросеть не ответила (%s) — встроенный разбор", llm_error(exc))
        return builtin
    profile = parse_llm_profile(raw, text, settings)
    if profile is None:
        log.warning("резюме: ответ нейросети не разобран — встроенный разбор")
        return builtin
    if not profile.directions:  # нейросеть не нашла — лучше слова, чем пусто
        profile = replace(profile, directions=builtin.directions)
    return profile


async def _ask(client: Any, text: str, settings: Settings) -> str:
    directions = "\n".join(
        f"- {name}: {DIRECTION_LABELS.get(name, name)}" for name in settings.directions
    )
    response = await client.messages.create(
        model=settings.llm_model,
        max_tokens=RESUME_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Направления:\n{directions}\n\n<resume>\n"
                f"{text[:RESUME_MAX_CHARS]}\n</resume>",
            }
        ],
    )
    if getattr(response, "stop_reason", None) in ("refusal", "max_tokens"):
        return ""
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _comparable(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold().strip()


def parse_llm_profile(raw: str, text: str, settings: Settings) -> ResumeProfile | None:
    """Ответ нейросети → профиль; всё, чего нет в резюме, отбрасывается. Не JSON — None."""
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if start != -1 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None
    resume = _comparable(text)

    directions = [d for d in _strings(data.get("directions")) if d in settings.directions]
    keywords = [
        k.strip()
        for k in _strings(data.get("keywords"))
        if 2 <= len(k.strip()) <= 40 and find_terms(text, [k.strip()])
    ]
    # периоды мест работы в тексте надёжнее нейросети: она берёт общий стаж hh.ru
    years = profession_years(text)
    if years is None:
        years = data.get("experience_years")
    if isinstance(years, bool) or not isinstance(years, int | float) or not 0 <= years <= 60:
        years = experience_years(text)
    facts = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        fact, source = item.get("text"), item.get("source")
        if not (isinstance(fact, str) and isinstance(source, str)):
            continue
        source = _LIST_MARK_RE.sub("", source).strip()
        if len(source) >= 5 and _comparable(source) in resume and fact.strip():
            facts.append(ResumeFact(text=fact.strip()[:MAX_FACT_CHARS], source=source))
    # «делал ботов» — только если сказано в резюме: иначе отклики будут врать (ADR 0005)
    built = data.get("built_bots_or_agents") is True and bool(find_terms(text, _BOT_WORDS))
    return ResumeProfile(
        directions=list(dict.fromkeys(directions)),
        keywords=list(dict.fromkeys(keywords))[:MAX_KEYWORDS],
        experience_years=round(float(years), 1),
        built_bots_or_agents=built,
        facts=facts[:MAX_FACTS],
        generator="llm",
    )


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
