"""Резюме для анкеты: текст из файлов и ссылок, разбор нейросетью с проверкой. Сети нет."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from freelance_bot.config import load_settings
from freelance_bot.resume import (
    HH_HINT,
    LINK_HINT,
    MAX_FILE_BYTES,
    ResumeError,
    ResumeProfile,
    analyze,
    builtin_profile,
    city,
    download_url,
    experience_years,
    extract_text,
    fetch_link,
    find_link,
    parse_llm_profile,
    profession,
    section_items,
    work_periods,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = (ROOT / "profile" / "resume_facts.example.txt").read_text(encoding="utf-8")
RESUME = "Иван Иванов\nОпыт работы — 1 год 10 месяцев\n" + "\n".join(
    line for line in EXAMPLE.splitlines() if not line.startswith("#")
)


@pytest.fixture
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def _docx(paragraphs: list[str]) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _pdf(lines: list[str]) -> bytes:
    """Минимальный PDF с текстом (латиница, Helvetica) — pypdf его читает."""
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({t}) '" for t in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


# ── файлы ────────────────────────────────────────────────────────────────────


def test_docx_text_keeps_paragraphs():
    text = extract_text(_docx(RESUME.splitlines()), "resume.docx")
    assert "Опыт работы — 1 год 10 месяцев" in text.splitlines()
    assert "FastAPI для новых API" in text


def test_pdf_text_is_extracted():
    lines = ["Python developer, work experience 3 years"] + [f"Built REST API number {i}"
                                                            for i in range(12)]  # fmt: skip
    text = extract_text(_pdf(lines), "cv.pdf")
    assert "work experience 3 years" in text
    assert experience_years(text) == 3.0


def test_format_is_sniffed_without_extension():
    assert "FastAPI" in extract_text(_docx(RESUME.splitlines()), "")


def test_txt_in_cp1251_is_read():
    assert "Писал сервисы" in extract_text(RESUME.encode("cp1251"), "resume.txt")


@pytest.mark.parametrize("name", ["resume.doc", "resume.rtf", "resume.odt"])
def test_old_formats_ask_to_resave(name):
    with pytest.raises(ResumeError, match="Сохраните резюме как PDF, DOCX или TXT"):
        extract_text(b"\xd0\xcf\x11\xe0" + b"0" * 500, name)


def test_scan_without_text_is_explained():
    with pytest.raises(ResumeError, match="скан"):
        extract_text(_pdf([""]), "scan.pdf")


def test_broken_files_give_user_error():
    with pytest.raises(ResumeError, match="PDF"):
        extract_text(b"%PDF-1.4 garbage", "cv.pdf")
    with pytest.raises(ResumeError, match="DOCX"):
        extract_text(b"PK\x03\x04 not a zip", "cv.docx")


def test_too_big_file_is_refused():
    with pytest.raises(ResumeError, match="больше 10 МБ"):
        extract_text(b"0" * (MAX_FILE_BYTES + 1), "cv.txt")


# ── ссылки ───────────────────────────────────────────────────────────────────


def test_find_link_strips_punctuation():
    assert find_link("моё резюме: https://disk.yandex.ru/d/abc).") == "https://disk.yandex.ru/d/abc"
    assert find_link("без ссылки") is None


def test_download_url_supported_services():
    assert download_url("https://docs.google.com/document/d/AbC-1_x/edit?usp=sharing") == (
        "https://docs.google.com/document/d/AbC-1_x/export?format=txt",
        "resume.txt",
    )
    assert download_url("https://drive.google.com/file/d/XyZ/view")[0] == (
        "https://drive.google.com/uc?export=download&id=XyZ"
    )
    assert download_url("https://drive.google.com/open?id=XyZ")[0].endswith("id=XyZ")
    assert download_url("https://disk.yandex.ru/d/abc")[0] == "yandex:https://disk.yandex.ru/d/abc"


@pytest.mark.parametrize(
    ("link", "hint"),
    [
        ("https://hh.ru/resume/0123abc", HH_HINT),
        ("https://spb.hh.ru/resume/0123abc", HH_HINT),
        ("https://example.com/cv.pdf", LINK_HINT),
        ("http://169.254.169.254/latest/meta-data", LINK_HINT),
        ("https://docs.google.com/spreadsheets/d/1", LINK_HINT),
    ],
)
def test_other_links_are_refused(link, hint):
    with pytest.raises(ResumeError) as err:
        download_url(link)
    assert str(err.value) == hint


async def test_google_doc_is_downloaded_as_text():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "docs.google.com":
            return httpx.Response(
                307, headers={"location": "https://doc-0.googleusercontent.com/x"}
            )
        return httpx.Response(200, content=RESUME.encode())

    text = await fetch_link(
        "https://docs.google.com/document/d/ID/edit", transport=httpx.MockTransport(handler)
    )
    assert "FastAPI" in text
    assert seen == [
        "https://docs.google.com/document/d/ID/export?format=txt",
        "https://doc-0.googleusercontent.com/x",
    ]


async def test_redirect_to_foreign_host_is_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "drive.google.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/admin"})
        raise AssertionError("запрос ушёл на чужой адрес")

    with pytest.raises(ResumeError) as err:
        await fetch_link("https://drive.google.com/file/d/ID/view",
                         transport=httpx.MockTransport(handler))  # fmt: skip
    assert str(err.value) == LINK_HINT


async def test_yandex_disk_public_file():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cloud-api.yandex.net":
            assert request.url.params["public_key"] == "https://disk.yandex.ru/d/abc"
            return httpx.Response(200, json={"href": "https://downloader.disk.yandex.ru/f"})
        assert request.url.host == "downloader.disk.yandex.ru"
        return httpx.Response(200, content=_docx(RESUME.splitlines()))

    text = await fetch_link("https://disk.yandex.ru/d/abc", transport=httpx.MockTransport(handler))
    assert "PostgreSQL" in text


async def test_closed_file_gives_access_hint():
    page = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<!DOCTYPE html><html>"))
    with pytest.raises(ResumeError, match="доступ"):
        await fetch_link("https://drive.google.com/file/d/ID/view", transport=page)
    denied = httpx.MockTransport(lambda r: httpx.Response(403))
    with pytest.raises(ResumeError, match="доступ"):
        await fetch_link("https://drive.google.com/file/d/ID/view", transport=denied)


# ── разбор ──────────────────────────────────────────────────────────────────


def test_builtin_profile_from_keywords(settings):
    p = builtin_profile(RESUME, settings)
    assert p.directions[0] == "backend"
    # слова как в резюме, не основы из settings.toml («postgres», «интеграции с»)
    assert "FastAPI" in p.keywords and "PostgreSQL" in p.keywords
    assert "postgres" not in p.keywords and not any(k.endswith(" с") for k in p.keywords)
    assert p.experience_years == 1.8
    assert p.built_bots_or_agents is False and p.facts == [] and p.generator == "builtin"


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Опыт работы — 1 год 10 месяцев", 1.8),
        ("Опыт работы 5 лет", 5.0),
        ("Опыт работы — 3 года 6 месяцев", 3.5),
        ("Опыт работы: 8 месяцев", 0.7),
        ("Work experience 2 years 6 months", 2.5),
        ("Стажа нет", 0.0),
    ],
)
def test_experience_years(text, years):
    assert experience_years(text) == years


# Резюме в виде hh.ru: общий стаж считает и работу не по профессии
HH_RESUME = """Сидоров Олег
Опыт работы — 7 лет 10 месяцев
Январь 2025 —
по настоящее время
1 год 10 месяцев
ООО Ромашка
Информационные технологии, разработка сайтов
Python-разработчик
— Писал REST API на FastAPI
Май 2019 —
Декабрь 2024
5 лет 8 месяцев
Кафе «Лето»
Администратор
— Вёл кассу и графики смен
Образование
Сентябрь 2015 — Июнь 2019
Колледж, программист
"""
TODAY = date(2026, 10, 25)


def test_experience_counts_only_it_jobs_not_hh_total():
    jobs = work_periods(HH_RESUME, TODAY)
    assert len(jobs) == 2  # учёба в «Образовании» — не место работы
    assert "Администратор" in jobs[1].text and "Колледж" not in jobs[1].text
    assert experience_years(HH_RESUME, TODAY) == 1.8  # январь 2025 — октябрь 2026


@pytest.mark.parametrize(
    ("text", "years"),
    [
        # пересечение периодов не считается дважды: ноябрь 2024 — октябрь 2026
        ("Опыт работы\n11.2024 – 08.2026\nBackend developer\n"
         "01.2026 – 10.2026\nPython, фриланс", 2.0),
        ("Work experience\nNov 2024 - present\nSoftware engineer, Python", 2.0),
        # не разработчик — стаж по всем местам работы, а не ноль
        ("Опыт работы\nМарт 2020 — Май 2022\nМенеджер по продажам", 2.2),
        ("Образование\nСентябрь 2016 — Июнь 2020\nПрограммист", 0.0),  # учёба, стажа нет
    ],
)  # fmt: skip
def test_experience_from_work_periods(text, years):
    assert experience_years(text, TODAY) == years


def test_llm_total_experience_is_replaced_by_work_periods(settings):
    text = HH_RESUME.replace("по настоящее время", "Октябрь 2026")
    p = parse_llm_profile(_llm_answer(experience_years=7.8), text, settings)
    assert p.experience_years == 1.8


def _llm_answer(**overrides) -> str:
    data = {
        "directions": ["backend", "automation", "space_flight"],
        "keywords": ["FastAPI", "Kafka", "Kubernetes"],
        "experience_years": 1.8,
        "built_bots_or_agents": False,
        "facts": [
            {"text": "писал сервисы на FastAPI",
             "source": "— Писал сервисы на Python: FastAPI для новых API, Flask для внутренних "
                       "инструментов"},
            {"text": "руководил командой из 10 человек", "source": "Руководил командой"},
        ],
    }  # fmt: skip
    data.update(overrides)
    return "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


def test_llm_profile_drops_what_resume_does_not_say(settings):
    p = parse_llm_profile(_llm_answer(), RESUME, settings)
    assert p.directions == ["backend", "automation"]  # неизвестное направление отброшено
    assert p.keywords == ["FastAPI", "Kafka"]  # Kubernetes в резюме нет
    assert [f.text for f in p.facts] == ["писал сервисы на FastAPI"]  # без выдуманного
    assert p.facts[0].source.startswith("Писал сервисы")  # маркер списка снят
    assert p.generator == "llm"


def test_llm_cannot_claim_bots_without_evidence(settings):
    text = RESUME.replace("LLM-ассистентов", "ассистентов")
    assert (
        parse_llm_profile(
            _llm_answer(built_bots_or_agents=True), text, settings
        ).built_bots_or_agents
        is False
    )  # noqa: E501
    bots = text + "\nДелал Telegram-ботов на aiogram"
    assert parse_llm_profile(
        _llm_answer(built_bots_or_agents=True), bots, settings
    ).built_bots_or_agents  # noqa: E501


def test_llm_bad_years_fall_back_to_resume(settings):
    p = parse_llm_profile(_llm_answer(experience_years=99), RESUME, settings)
    assert p.experience_years == 1.8


def test_llm_garbage_is_none(settings):
    assert parse_llm_profile("Не могу помочь", RESUME, settings) is None
    assert parse_llm_profile("{oops", RESUME, settings) is None


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=self.answer)
        return SimpleNamespace(stop_reason="end_turn", content=[text])


async def test_analyze_uses_llm_and_sends_only_head_of_resume(settings):
    client = FakeClient(_llm_answer())
    p = await analyze(RESUME + "x" * 20_000, settings, client)
    assert p.generator == "llm" and p.keywords == ["FastAPI", "Kafka"]
    sent = client.calls[0]["messages"][0]["content"]
    assert len(sent) < 6000 and "backend:" in sent


async def test_analyze_falls_back_to_builtin(settings):
    failing = FakeClient(error=RuntimeError())
    assert (await analyze(RESUME, settings, failing)).generator == "builtin"
    assert (await analyze(RESUME, settings, FakeClient("нет"))).generator == "builtin"
    assert (await analyze(RESUME, settings)).generator == "builtin"  # ключей нет


async def test_llm_without_directions_gets_builtin_ones(settings):
    p = await analyze(RESUME, settings, FakeClient(_llm_answer(directions=[])))
    assert p.generator == "llm" and p.directions == builtin_profile(RESUME, settings).directions


# ── 10 вымышленных резюме разных профессий и вёрстки (текст как его отдаёт pypdf) ──

RESUMES = ROOT / "tests" / "fixtures" / "resumes"
CASES = {
    # файл: (профессия, город, стаж, должности мест работы, навыков, образование)
    "01_cafe_admin": ("Администратор кофейни", "", 4.0, ["Администратор кофейни"], 7, []),
    "02_sales_assistant": ("Продавец-консультант", "Липецк", 5.2,
                           ["Продавец-консультант — Магазин товаров для дома",
                            "Кассир — Супермаркет «У дома»"], 5,
                           ["Колледж сервиса, коммерция · 2021"]),
    "03_warehouse_picker": ("Комплектовщик склада", "Воронеж", 4.3,
                            ["Комплектовщик — Склад «РегионТорг»",
                             "Рабочий склада — ООО «Снабжение»"], 5,
                            ["Среднее общее образование, 2022"]),
    "04_courier": ("Курьер на личном автомобиле", "Липецк", 4.0,
                   ["Курьер — Служба доставки «Быстро»",
                    "Водитель-экспедитор — Магазин «Фермерский»"], 4,
                   ["Среднее профессиональное образование · логистика · 2022"]),
    "05_cleaner": ("Уборщик помещений", "Елец", 6.5,
                   ["Уборщик служебных помещений — Бизнес-центр «Север»",
                    "Горничная — Гостиница «Встреча»"], 4, ["Среднее общее образование"]),
    "06_kindergarten_teacher": ("Воспитатель детского сада", "Липецк", 8.2,
                                ["Воспитатель — Детский сад № 18",
                                 "Помощник воспитателя — Детский сад «Ромашка»"], 4,
                                ["Педагогический колледж · дошкольное образование · 2018"]),
    "07_accountant": ("Бухгалтер на первичную документацию", "Тамбов", 3.8,
                      ["Помощник бухгалтера — Торговая компания «Парус»",
                       "Оператор учёта — Складская компания «Маяк»"], 5,
                      ["Экономический колледж · бухгалтерский учёт · 2022"]),
    "08_cook": ("Повар горячего цеха", "Воронеж", 6.8,
                ["Повар — Кафе «Тёплый хлеб»", "Повар-универсал — Столовая «Линия»"], 5,
                ["Профессиональное училище · поварское дело · 2020"]),
    "09_hotel_admin": ("Администратор гостиницы", "Липецк", 6.2,
                       ["Администратор — Гостиница «Центральная»",
                        "Оператор бронирования — Гостевой дом «Липа»"], 5,
                       ["Курсы гостиничного сервиса · 2020"]),
    # второе место — «2020 (сезонно)»: одним годом, сезон — 3 месяца (отчёт QA 2026-10-25)
    "10_florist": ("Флорист", "Курск", 5.2, ["Флорист — Салон цветов «Лепесток»",
                                             "Помощник продавца — Цветочный павильон"], 4,
                   ["Курсы флористики · 2021"]),
}  # fmt: skip


@pytest.mark.parametrize("name", CASES)
def test_resumes_of_other_professions(name, settings):
    title, town, years, roles, skills, education = CASES[name]
    text = extract_text((RESUMES / f"{name}.txt").read_bytes(), f"{name}.txt")
    p = builtin_profile(text, settings, TODAY)
    assert (p.profession, p.city, p.experience_years) == (title, town, years)
    assert [job.title for job in work_periods(text, TODAY)] == roles
    assert len(p.skills) == skills and p.education == education
    # подписи-предложения и контакты не попадают ни в навыки, ни в образование
    assert not any("вымышлен" in item or "@" in item for item in p.skills + p.education)


def test_other_professions_get_no_it_directions(settings):
    for path in sorted(RESUMES.glob("*.txt")):
        text = extract_text(path.read_bytes(), path.name)
        assert builtin_profile(text, settings, TODAY).directions == [], path.name  # «Excel»


def test_resume_pdf_end_to_end(settings):
    text = extract_text((RESUMES / "04_courier.pdf").read_bytes(), "resume.pdf")
    assert "Планирую маршрут, бережно" in text  # пробел pypdf перед запятой убран
    p = builtin_profile(text, settings, TODAY)
    assert p.jobs == ["Курьер — Служба доставки «Быстро» (2024–2026)",
                      "Водитель-экспедитор — Магазин «Фермерский» (2022–2024)"]  # fmt: skip
    assert "Навигация" in p.skills and p.experience_years == 4.0


@pytest.mark.parametrize(
    ("period", "months"),
    [
        ("Май 2023 — настоящее время", 42),  # по октябрь 2026 включительно
        ("Апр 2022 — Сен 2026", 54),
        ("С 03/2022 по 08/2026", 54),
        ("09.2020 — по н.в.", 74),
        ("01.02.2024 - 01.03.2025", 14),
        ("2022–2026", 48),  # одни годы: по начало последнего года
        ("2021 / 2026", 60),
        ("2023–н.в.", 46),
        ("2022 — 2022", 12),
    ],
)
def test_period_formats(period, months):
    (job,) = work_periods(f"Опыт работы\nПовар — Кафе\n{period}\nГотовил блюда", TODAY)
    assert job.end - job.start + 1 == months and job.title == "Повар — Кафе"


def test_single_year_job_counts_and_education_does_not():
    text = "Опыт работы\nПовар\n2020 (сезонно)\nОбучение\nКурсы, 2019 — 2020"
    [job] = work_periods(text, TODAY)
    assert (job.title, job.period, job.end - job.start + 1) == ("Повар", "2020, сезонно", 3)
    whole = work_periods("Опыт работы\nКассир — Магазин\n2019\nНавыки\nКасса", TODAY)
    assert [(j.label, j.end - j.start + 1) for j in whole] == [("Кассир — Магазин (2019)", 12)]
    # год в «О себе» или в строке с текстом — не место работы
    assert work_periods("О себе\n2020\nОпыт работы\nВ 2020 году работал", TODAY) == []


def test_title_under_period_in_hh_layout():
    jobs = work_periods(HH_RESUME, TODAY)
    assert [job.title for job in jobs] == ["ООО Ромашка", "Кафе «Лето»"]
    assert not any(job.role for job in jobs)  # компания, а не должность


def test_hh_header_fields():
    text = ("Сидоров Олег\nМужчина, 30 лет\nПроживает: Воронеж\n"
            "Желаемая должность\nPython-разработчик\nКлючевые навыки\n"
            "Знание языков Английский — A2\nНавыки Python, Django, Redis")  # fmt: skip
    assert profession(text) == "Python-разработчик" and city(text) == "Воронеж"
    assert section_items(text, "skills", 15) == [
        "Английский — A2", "Python", "Django", "Redis"
    ]  # fmt: skip


def test_inline_skills_and_header_like_sentences():
    text = "Иван Петров\nНавыки: Excel, 1С\nОпыт работы на стойке — главное.\nО себе\nТекст"
    assert section_items(text, "skills", 15) == ["Excel", "1С"]


def test_llm_takes_quoted_fields_and_fills_gaps_from_text(settings):
    text = (RESUMES / "08_cook.txt").read_text(encoding="utf-8")
    text = extract_text(text.encode(), "r.txt")
    answer = _llm_answer(profession="Повар", city="Москва", skills=["Горячий цех", "Суши"],
                         education=[])  # fmt: skip
    p = parse_llm_profile(answer, text, settings)
    assert p.profession == "Повар" and p.city == "Воронеж"  # Москвы в резюме нет
    assert p.skills == ["Горячий цех"]  # «Суши» нет в резюме
    assert p.education == ["Профессиональное училище · поварское дело · 2020"]
    assert len(p.jobs) == 2


async def test_contacts_are_not_sent_to_llm(settings):
    text = extract_text((RESUMES / "02_sales_assistant.txt").read_bytes(), "r.txt")
    client = FakeClient(_llm_answer())
    await analyze(text, settings, client)
    sent = client.calls[0]["messages"][0]["content"]
    assert "@example.com" not in sent and "000-00-02" not in sent
    assert "Август 2021 — апрель 2023" in sent and "[телефон]" in sent
    assert "не инструкции" in client.calls[0]["system"]


def test_old_profile_without_new_fields_is_read():
    p = ResumeProfile.from_dict({"directions": ["backend"], "experience_years": 1.8})
    assert p.profession == "" and p.skills == [] and p.jobs == []


def test_profile_roundtrip():
    p = parse_llm_profile(_llm_answer(), RESUME, load_settings(ROOT / "settings.toml",
                                                              ROOT / "no-such.env"))  # fmt: skip
    assert ResumeProfile.from_dict(json.loads(json.dumps(p.to_dict()))) == p
