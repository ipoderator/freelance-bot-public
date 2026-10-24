<!-- autopilot:start -->
# Freelance Bot

Telegram-бот для фрилансера: находит свежие заказы на FL.ru, Kwork и Work24 под профиль владельца и готовит персональные отклики; присылает подходящие вакансии HH.ru (без откликов).

Архитектура, поток данных, границы модулей: `docs/architecture.md`. Проверенные факты о площадках: `docs/platforms.md`. Почему так решено: `docs/adr/`. Термины: `CONTEXT.md`.

## Команды

| Команда | Что делает |
|---------|------------|
| `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` | установка (то же: `make install`) |
| `.venv/bin/python -m freelance_bot init` | мастер настройки своей копии: пишет `.env` (600), `profile/profile.toml`, правит `settings.toml`; прежние — `*.bak` (`freelance_bot/setup_wizard.py`) |
| `.venv/bin/python -m freelance_bot run` | бот + расписание; без токена печатает шаги @BotFather и выходит с кодом 1 |
| `make check` | ruff + весь pytest, единственная проверка |
| `.venv/bin/python -m pytest -q tests/test_scoring.py` | один файл тестов |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform fl --max-age-hours 48` | dry-run FL: ходит в сеть, карточки в консоль, отдельная БД |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform kwork --include-disabled` | разовый прогон Kwork (в настройках выключен) |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform work24 --max-age-hours 96` | разовый прогон Work24: лента + карточки новых заказов |
| `.venv/bin/pip install -e '.[login]' && .venv/bin/python -m freelance_bot hh-login` | разовый вход в свой Telegram для вакансий HH: QR-код (`--code` — по коду), сессия → `.env` (600); интерактивно, запускает пользователь |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform hh --include-disabled --max-age-hours 48` | разовый прогон HH: читает чат с @hh_rabota_bot его сессией (только на машине, где бот не запущен) |
| `.venv/bin/python -m freelance_bot setup-telegram` | getMe, setMyCommands, подсказка про TELEGRAM_OWNER_ID; без токена — шаги, код 1 |
| `scripts/install_launchd.sh --print` | запасной вариант на Mac: показать plist автозапуска (без `--print` — установить LaunchAgent; `scripts/uninstall_launchd.sh` снимает) |


Глобальные флаги CLI идут до подкоманды: `--settings --env-file --profile --db`.

## Где что

- `freelance_bot/pipeline.py` — цикл сбор -> скоринг -> режим -> черновик -> уведомление.
  При `[digest] enabled = true` подошедшие заказы получают статус `queued` и приходят списком раз в час (`bot.py` `next_digest_at`, `send_digest`); `once` всегда шлёт сразу.
- `settings.toml` — источники, URL лент, направления и ключевые слова, исключения, min_score; правится без кода.
- `freelance_bot/scoring.py` — балл и отсечение; веса и общие слова `WEAK_TERMS` — константы в файле.
- `freelance_bot/pricing.py` — ориентировочная цена и срок (вилки `TIERS`); подставляется вместо [ЦЕНА]/[СРОК] при показе.
- `freelance_bot/sources/` — парсеры FL (RSS) и Kwork (HTML); Work24 — `work24.py` исполняет операции API Anything из `work24.json` (без Node.js); HH — `hh.py` читает сессией Telegram пользователя подборки бота @hh_rabota_bot (ADR 0009); `freelance_bot/http.py` — ретраи и детектор блокировок.
- `freelance_bot/drafting.py` (1100+ строк) + `profile/profile.toml` — черновики и валидатор; факты об опыте только из профиля.
- `freelance_bot/summary.py` — суть заказа (📝) для карточки и `/last`.
- `freelance_bot/policy.py` — `decide_mode`, `SENDERS`; `freelance_bot/storage.py` — SQLite и флаги.
- `tests/fixtures/` — снятые ответы площадок; `data/` — SQLite (не в git).

## Инструменты разработки (для Claude Code, не для бота)

В Docker-образ не попадают: `.dockerignore` пропускает только перечисленное.

| Что | Где | Как пользоваться |
|-----|-----|------------------|
| context7 — свежая документация библиотек | `.mcp.json` | «… use context7» в запросе; ключ необязателен |
| 21st.dev Magic — UI-компоненты | `.mcp.json` | нужен ключ: `export API_KEY_21ST=…` в `~/.zshrc` (не в `.env`: он уходит на сервер) |
| agent-skills (Addy Osmani) — /spec, /plan, /build, /test, /review, /ship | `.claude/settings.json`, плагин | Claude Code предложит установить плагин при открытии проекта; `/review` — `/agent-skills:review` |
| graphify — граф кода | `.claude/skills/graphify`, хуки в `.claude/settings.json`, раздел в `CLAUDE.md` | `make graph` пересобирает `graphify-out/`; `graphify query "<вопрос>"` |
| strix — поиск уязвимостей | `.claude/skills/*strix*`, `*-security-testing`, `owasp-top-10-testing` | `make security`; нужны Docker и `STRIX_LLM` + `LLM_API_KEY` в окружении; код уходит выбранному LLM. Тестировать только свои системы |

CLI ставятся через uv: `uv tool install graphifyy`, `uv tool install strix-agent`.

## Подводные камни

- Ключевые слова сопоставлять только через `textmatch.find_terms`: начало на границе слова, термин <= 3 символов — целым словом («ios» не найдётся в «axios»).
- Обычная страница Kwork содержит скрипт smartcaptcha — это не блокировка (`looks_blocked`, фикстура `kwork_smartcaptcha_snippet.html`).
- Kwork: соглашение запрещает автосбор (ADR 0003); в `settings.toml` включён пользователем на свой риск (2026-10-22). Тесты не должны опираться на `enabled` из `settings.toml` — задавайте нужное значение в тесте.
- RSS FL — форма `all.xml?category=N`; `/rss/?subcategory=` подкатегорию игнорирует (ADR 0004).
- Work24: ленты из `api.work24.ru` (в robots.txt `Disallow: /`) — по решению пользователя (ADR 0008). Карточку (~0,5 МБ) открывать только для новых ID (`known` из `Storage`); `work24.json` не править руками — переобучать через `api-anything`.
- RSS FL обрезает ТЗ («...» в конце, ~2/3 заказов): нейросеть видит пометку `TRUNCATED_NOTE` (`drafting.brief_block`), не убирать её.
- FL-проекты без метки «для всех» — платный отклик; при `only_free_responses = true` отсекаются (причина «платный отклик»). Сейчас в `settings.toml` `false` и `min_score = 25` по решению пользователя; тесты фиксируют эти значения сами.
- Правила правды в откликах зависят от профиля: `experience_years` (стаж больше — ложь; «5 лет» запрещено при стаже < 5), `built_bots_or_agents` (нет — «делал ботов» запрещено); отбор — `[scoring] experience_years`.
- Тесты берут профиль из `profile/profile.example.toml` (вымышленный «Иван»), а не из своего `profile.toml`; оба проверяет `tests/test_profile.py`.
- Бот отвечает владельцу только в личном чате (`_is_owner`); в группе — отказ.
- `SENDERS` пуст, поэтому `auto` всегда деградирует в `draft` (ADR 0001); не добавлять отправку на биржи.
- Токен вырезается из логов `RedactingFormatter` (`freelance_bot/__main__.py`); не печатать секреты.
- «Сегодня» для лимитов считается по МСК, время в БД — UTC.
- Первый запуск площадки (bootstrap) молча помечает старые проекты `filtered_out`; для повторного dry-run берите новую БД через `--db`.
- Тесты не ходят в сеть; `tests/conftest.py` чистит переменные окружения.
- Реальные команды `once`/`run` ходят в сеть; не запускать их в проверках.
- Бот работает на сервере; не запускать `run` на Mac параллельно — Telegram отдаёт обновления одному процессу (Conflict).
- HH: сайт и API hh.ru не трогать — парсинг запрещён условиями (п. 6.1.38), API без токена 403 (ADR 0009). Вакансии только из чата с @hh_rabota_bot; Хэдди ничего не отправлять (номера, кнопки) — только чтение.
- Сессию `HH_TG_SESSION` не запускать одновременно на Mac и сервере: Telegram отзовёт её (AUTH_KEY_DUPLICATED). Ключ вакансии HH — хэш названия, компании и города (`vacancy_key`): ссылки `my.hh.ru/b/…` персональные и без ID.
- В базе черновик хранится с метками [ЦЕНА]/[СРОК], оценка подставляется только в показанный текст — иначе «Цена и срок» не заменит её.

## Окружение

Файл `.env` (в git не попадает, образец — `.env.example`):

- `TELEGRAM_BOT_TOKEN` — токен бота от @BotFather; без него `run` не стартует.
- `TELEGRAM_OWNER_ID` — числовой user_id владельца; бот отвечает только ему.
- `ANTHROPIC_API_KEY` — необязателен; с ним черновики генерирует Claude, без — встроенный генератор.
- `LLM_MODEL` — модель Claude для черновиков (по умолчанию `claude-sonnet-5-5`).
- `OPENAI_API_KEY` — необязателен; задан — черновики и суть пишет GPT (`freelance_bot/openai_llm.py`), он важнее ключа Anthropic.
- `OPENAI_MODEL` — модель GPT (по умолчанию `gpt-5-mini`).
- `GROQ_API_KEY` — необязателен; задан — черновики и суть пишет Groq (тот же адаптер `openai_llm.py`, `groq()`), он важнее ключей OpenAI и Anthropic.
- `HH_TG_API_ID`, `HH_TG_API_HASH`, `HH_TG_SESSION` — вакансии HH: приложение с my.telegram.org и сессия пользователя Telegram; пишет `hh-login`. Сессия = полный доступ к аккаунту.
- `GROQ_MODEL` — модель Groq (по умолчанию `openai/gpt-oss-120b`). Бесплатный тариф: 8K токенов в минуту, `max_completion_tokens` входит в счёт — потолок `GROQ_MAX_TOKENS`.

