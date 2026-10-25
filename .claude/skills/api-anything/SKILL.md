---
name: api-anything
description: Создание, переобучение и проверка операций чтения для бирж через goodnight000/api-anything — «подключи площадку через api-anything», «переобучи операции Work24», «лента Freelance.ru перестала разбираться» (SourceFormatError), «сними операцию для новой биржи», «обнови work-zilla.json». Только чтение, только операции, которые бот сам исполняет без браузера; откликов и других записей на биржах не делает.
license: MIT
---

# Операции api-anything для бирж

Адаптация навыка `api-anything` (MIT, © 2026 Tianjun Zheng, `LICENSE` рядом) под этот
бот. Сам api-anything в бот не входит: бот исполняет готовый JSON операций на Python
(`freelance_bot/sources/api_anything.py`, ADR 0008, 0010). Файлы операций:

| Площадка | Файл | Источник | Тесты |
|---|---|---|---|
| Work24 | `freelance_bot/sources/work24.json` | `sources/work24.py` | `tests/test_sources_work24.py` |
| Freelance.ru | `freelance_bot/sources/freelance-ru.json` | `sources/freelance_ru.py` | `tests/test_sources_freelance_ru.py` |
| Work-zilla | `freelance_bot/sources/work-zilla.json` | `sources/workzilla.py` | `tests/test_sources_workzilla.py` |

Общий исполнитель — `tests/test_api_anything.py`.

## Жёсткие правила

- **Бот принимает только** операции `readOnly: true`, метод `GET`, `minTier` 1, без
  `volatile`, ответ `json` или `html` (`load_operations`). Операция, которой нужен браузер
  (tier 2–3), боту бесполезна: он её отклонит `ConfigError`. Смотреть `warnings` у `add` —
  «minTier 3» значит «не годится».
- **Никаких записей:** не учить операции с `--write`, не вызывать с `--allow-writes`.
  Отклики на биржах бот не отправляет (ADR 0001).
- **JSON в `freelance_bot/sources/` руками не править** — только заменить экспортом.
- **Правила площадки до обучения:** robots.txt и условия — в `docs/platforms.md`; новый
  источник или закрытый robots.txt адрес — решение пользователя и ADR (навык `adr`).
- **Сессии и cookie** не печатать, в спецификации, аргументы и чат не вставлять. Вход —
  только `api-anything login <site>` самим пользователем; сессию Work-zilla в бот переносит
  `.venv/bin/python -m freelance_bot workzilla-session --from <API_ANYTHING_HOME>/sessions/work-zilla.json`
  (пишет `.env`, значения не печатает).
- **`publish` в общий реестр** — только с явного согласия пользователя после показа
  `files`, `warnings` и `personal` из предпросмотра.
- **Сбой** (`class`: `auth`, `rate`, `blocked`, `drift`) — один раз выполнить `next`, если
  он не требует пользователя; дальше остановиться и сообщить. Каждый повтор — запрос к
  бирже от имени пользователя.

## Где CLI

`api-anything` не стоит глобально. Варианты: `npx -y github:goodnight000/api-anything <команда>`
(ставит из сети) или локальный рантайм. Данные — в `API_ANYTHING_HOME`, у каждой площадки
своя папка.

## Переобучить операцию (площадка сменила формат)

Признак — `SourceFormatError` в `last_error:<платформа>` и предупреждение после трёх сбоев
подряд (навык `known-issue` — записать).

1. `api-anything ops <site>` — какие операции и примеры есть; `notes` прочитать.
2. `api-anything verify <site>` — какие операции сломаны. `ok: false`, `class: drift` —
   один раз `api-anything heal <site> <op>`; не помогло — переснять: `capture <страница>
   --example k=v --outline`, затем `add <site> <op> --from <captureId> ...` с новым
   `--extract`/`--pick` или `--html`. Два набора примеров (`--example`, `--example2`).
3. `api-anything call <site> <op> k=<значение не из примера>` — данные совпадают с
   тем, что видно на странице.
4. **Экспорт в черновик**, не сразу в пакет:
   ```bash
   api-anything export <site> --keep-examples --out <scratchpad>/<site>.json
   ```
   `--keep-examples` — если в текущем файле бота у параметров есть `example` (сейчас у
   `work24.json` и `work-zilla.json` есть, у `freelance-ru.json` — нет); иначе без флага.
   Проверка флага: на неизменённой площадке экспорт должен совпасть с файлом бота байт в
   байт (`cmp <scratchpad>/<site>.json freelance_bot/sources/<site>.json`).
5. `diff` черновика с файлом бота: поменялись только запросы и разбор нужных операций; новых
   операций записи и `minTier` > 1 нет.
6. Заменить файл, обновить фикстуру ответа в `tests/fixtures/`, если поменялся формат, и
   прогнать:
   ```bash
   .venv/bin/python -m pytest -q tests/test_api_anything.py tests/test_sources_work24.py tests/test_sources_freelance_ru.py tests/test_sources_workzilla.py
   make check
   ```
7. Прогон по сети — только по просьбе пользователя и с отдельной базой:
   `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform <платформа> --include-disabled`.

## Новая биржа

1. Правила площадки (robots.txt, условия) → `docs/platforms.md`; решение пользователя → ADR.
2. Договориться об операциях: `listX(page) -> id, title, url, published, budget…`,
   `getX(id) -> description, status`. Только то, что видно без входа, если пользователь не
   решил иначе.
3. Обучить, проверить и экспортировать, как выше (шаги 1–5).
4. В боте: `freelance_bot/sources/<имя>.py` по образцу `work24.py` (лента → карточки только
   для новых ID из `Storage`, лимиты страниц и карточек за цикл), раздел
   `[sources.<имя>]` в `settings.toml` (выключен, пока не проверено), платформа в
   `models.PLATFORMS` и `telegram_ui.PLATFORM_NAMES`, тесты на снятой фикстуре (без сети).
5. Строка о площадке в `AGENTS.md` (команда разового прогона, ловушки).
