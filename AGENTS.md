<!-- autopilot:start -->
# Freelance Bot

Telegram-бот для фрилансера: находит свежие заказы на FL.ru, Kwork, Work24, Freelance.ru и Work-zilla под профиль владельца и готовит персональные отклики; присылает подходящие вакансии HH.ru, SuperJob и «Работы России» (без откликов).

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
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform freelance_ru --max-age-hours 48` | разовый прогон Freelance.ru: первая страница /task + карточки новых заданий IT-разделов |
| `.venv/bin/python -m freelance_bot workzilla-session --from <API_ANYTHING_HOME>/sessions/work-zilla.json` | сессия Work-zilla из api-anything → `WORKZILLA_COOKIE`, `WORKZILLA_AGENT_ID` в `.env` (600); значения не печатает |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform workzilla --include-disabled` | разовый прогон Work-zilla: пока лента демонстрационная — ошибка «лента демонстрационная», ничего не сохраняется |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform superjob --include-disabled --max-age-hours 48` | разовый прогон SuperJob (официальный API, нужен `SUPERJOB_API_KEY`) |
| `.venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once --dry-run --console --platform trudvsem` | разовый прогон «Работы России»: ищет только под пройденную анкету владельца в этой базе, без неё — 0 |
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
- `freelance_bot/sources/` — парсеры FL (RSS) и Kwork (HTML); Work24, Freelance.ru, Work-zilla — `work24.py`, `freelance_ru.py`, `workzilla.py` исполняют операции API Anything из `work24.json`, `freelance-ru.json`, `work-zilla.json` через общий `api_anything.py` (без Node.js); HH — `hh.py` читает сессией Telegram пользователя подборки бота @hh_rabota_bot (ADR 0009); SuperJob — `superjob.py`, официальный API с ключом приложения (ADR 0012); «Работа России» — `trudvsem.py`, открытые данные без ключа (ADR 0015); `freelance_bot/http.py` — ретраи и детектор блокировок.
- `freelance_bot/drafting.py` (1100+ строк) + `profile/profile.toml` — черновики и валидатор; факты об опыте только из профиля.
- `freelance_bot/summary.py` — суть заказа (📝) для карточки и `/last`.
- `freelance_bot/policy.py` — `decide_mode`, `SENDERS`; `freelance_bot/storage.py` — SQLite и флаги.
- `freelance_bot/learning.py` — самообучение (ADR 0013): «👎 Не подходит» + причина → правило в таблице `lessons`, поправка к баллу или отсечение (тема и уровень — по названию; навыки — по всему тексту; стаж «опыт от N лет» — по описанию, `freelance_bot/experience.py`); `/learned` и кнопка «🧠 Обучение» — список правил и «забыть».
- `freelance_bot/onboarding.py` — анкета публичного бота (ADR 0011): согласие → ФИО → обращение → резюме → направления → площадки → другие города и удалёнка (если выбраны вакансии), таблица `users`, частота уведомлений — `freelance_bot/schedule.py` (раз в час 9–21 по умолчанию или свой тайминг); `freelance_bot/resume.py` — текст из PDF/DOCX/TXT и ссылок Google/Яндекс Диска, разбор нейросетью с проверкой по тексту резюме или встроенный (профессия, город, стаж, места работы, навыки, образование; образцы разной вёрстки — `tests/fixtures/resumes/`). Рассылка по анкетам — этап 2, ещё не сделан.
- `freelance_bot/matching.py` — подбор под анкету по резюме (ADR 0014): живой поиск SuperJob и «Работы России» по профессии и городу (`VacancySearch`), он же при сборе под анкету владельца (`AnketaVacancies`), отсечение вне анкеты (`outside_anketa`), отсечение того, что резюме не выполняет (`mismatch`: стаж, город и удалёнка по выбору в анкете, профессия, IT-вакансия не разработчику), порядок и повторы (`rank`); не разработчику — без IT-направлений и бирж.
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

## Навыки Claude Code (`.claude/skills/`)

Claude применяет их сам по описанию; вызвать явно — `/<имя>`.

| Навык | Когда |
|---|---|
| `lictor-security-check` | перед деплоем, публикацией, включением `[public]`; «проверь безопасность», «нет ли утечек» — секреты, сессии бирж и HH, ключи нейросетей, логи, текст заказов в промптах и HTML, запрет записи на биржи. Только чтение |
| `known-issue` | площадка перестала отдавать заказы, сессия или ключ отозваны, лента сменила формат, ограничение robots.txt/условий — куда записать и как закрыть |
| `adr` | выбор или пересмотр способа работы: новый источник, отказ от площадки, данные, нейросеть, публичный режим → `docs/adr/` |
| `api-anything` | снять, переобучить, проверить операции чтения для бирж (`freelance_bot/sources/*.json`): только GET tier 1, без записей |
| `db-migration` | новая таблица, столбец, индекс в SQLite (`storage.MIGRATIONS`) |
| `close-task` | конец правки: `make check`, `graphify update .`, документация, записи о правке |
| `commit-push` | «коммит и пуш» — только по просьбе пользователя |

Не заведены: `ci-triage` — CI нет; `security-review` — есть встроенный `/security-review`,
`lictor-security-check` и навыки strix.

## Подводные камни

- Ключевые слова сопоставлять только через `textmatch.find_terms`: начало на границе слова, термин <= 3 символов — целым словом («ios» не найдётся в «axios»).
- Обычная страница Kwork содержит скрипт smartcaptcha — это не блокировка (`looks_blocked`, фикстура `kwork_smartcaptcha_snippet.html`).
- Kwork: соглашение запрещает автосбор (ADR 0003); в `settings.toml` включён пользователем на свой риск (2026-10-22). Тесты не должны опираться на `enabled` из `settings.toml` — задавайте нужное значение в тесте.
- RSS FL — форма `all.xml?category=N`; `/rss/?subcategory=` подкатегорию игнорирует (ADR 0004).
- Work24: ленты из `api.work24.ru` (в robots.txt `Disallow: /`) — по решению пользователя (ADR 0008). Карточку (~0,5 МБ) открывать только для новых ID (`known` из `Storage`); `work24.json` не править руками — переобучать через `api-anything`.
- Freelance.ru (ADR 0010): только `/task` без `?page=` (robots.txt); лента общая для всех разделов — карточки открываются лишь для `categories`; «Только для Премиум» пропускать (текст закрыт); контакты заказчика вырезаются из текста (`_without_contacts`), иначе «Телеграм: @ник» засчитывается направлением Telegram.
- Work-zilla (ADR 0010): задания разбирать только при `isStaticOrders == false`; демо-лента и ответ без поля — ошибка источника, в базу ничего. Источник выключен, настоящая лента вживую не проверялась. Cookie сессии — только в своём `Fetcher` и в `RedactingFormatter`; не печатать.
- RSS FL обрезает ТЗ («...» в конце, ~2/3 заказов): нейросеть видит пометку `TRUNCATED_NOTE` (`drafting.brief_block`), не убирать её.
- FL-проекты без метки «для всех» — платный отклик; при `only_free_responses = true` отсекаются (причина «платный отклик»). Сейчас в `settings.toml` `false` и `min_score = 25` по решению пользователя; тесты фиксируют эти значения сами.
- Правила правды в откликах зависят от профиля: `experience_years` (стаж больше — ложь; «5 лет» запрещено при стаже < 5), `built_bots_or_agents` (нет — «делал ботов» запрещено); отбор — `[scoring] experience_years`.
- Тесты берут профиль из `profile/profile.example.toml` (вымышленный «Иван»), а не из своего `profile.toml`; оба проверяет `tests/test_profile.py`.
- Бот отвечает владельцу только в личном чате (`_is_owner`); в группе — отказ.
- Публичный режим `[public] enabled` (ADR 0011): посторонним в личке — анкета (`_is_public`), команды владельца им закрыты. На сервере выключен до этапа 2 (рассылка). У владельца `/start` тоже сразу открывает анкету (заново — `/signup`), его меню — `/menu`; `profile.toml` анкета не трогает, но пройдена анкета — меню, `/last` и дайджест владельца отбирают по ней (`Pipeline.scoring_settings`, `anketa`; ADR 0015). Kwork в анкете не предлагать («давай пока без kwork»).
- Стаж из резюме — по периодам мест работы в IT (`resume.profession_years`), а не по общей строке hh «Опыт работы — N лет»: она считает и работу не по профессии. Найдены периоды — они важнее ответа нейросети; поправить вручную — кнопка «✏️ Стаж» в анкете.
- Ссылки на резюме — только Google Docs/Диск и Яндекс Диск (`resume.ALLOWED_HOST_SUFFIXES`): адрес присылает посторонний, а запрос идёт с сервера (SSRF). Резюме hh.ru по ссылке не скачивать. Файл резюме не хранить — только выжимку; в тестах — вымышленные ФИО (проверка утечек ловит настоящие).
- `SENDERS` пуст, поэтому `auto` всегда деградирует в `draft` (ADR 0001); не добавлять отправку на биржи.
- Токен вырезается из логов `RedactingFormatter` (`freelance_bot/__main__.py`); не печатать секреты.
- «Сегодня» для лимитов считается по МСК, время в БД — UTC.
- Первый запуск площадки (bootstrap) молча помечает старые проекты `filtered_out`; для повторного dry-run берите новую БД через `--db`.
- Тесты не ходят в сеть; `tests/conftest.py` чистит переменные окружения.
- Реальные команды `once`/`run` ходят в сеть; не запускать их в проверках.
- Бот работает на сервере; не запускать `run` на Mac параллельно — Telegram отдаёт обновления одному процессу (Conflict).
- «Работа России» (ADR 0015): фильтра по городу нет — регион узнаётся у API по названию города (`trudvsem.region_for`); город вакансии брать из адреса («г Елец»), не из региона — иначе «липецк» найдётся в «Липецкая область». Запрос ~5 с: площадки поиска под анкету опрашиваются параллельно, у «Работы России» 2 слова и до 2 страниц. robots.txt хоста `Disallow: /` — решение пользователя.
- SuperJob API отдаёт не больше 40 вакансий за запрос: на `count=100` — 40 и `more: false`, остальное теряется. Листать по `PAGE_SIZE = 40` до `more: false` (`sources/superjob.py`).
- Требуемый стаж в тексте — только `experience.required_years` (его зовут скоринг и обучение): у вилки «1–3 года» требование — нижняя граница, свою регулярку не заводить.
- Отбор с отметками пользователя — только через `Pipeline.scoring_settings()` (или `replace(settings, lessons=…)`): голый `self.settings` правил из `lessons` не видит (ADR 0013).
- Свой срок свежести площадки — `[sources.<p>] max_age_hours` (у SuperJob 72 ч); читать через `Settings.max_age_for(platform)`, а не `settings.max_age_hours`. Флаг `--max-age-hours` у `once` перекрывает и его.
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
- `WORKZILLA_COOKIE`, `WORKZILLA_AGENT_ID` — сессия аккаунта исполнителя Work-zilla (cookie `Bearer` и заголовок `agentid`); пишет `workzilla-session`. Cookie = вход в аккаунт.
- `SUPERJOB_API_KEY` — ключ приложения API SuperJob (api.superjob.ru/register/, заголовок `X-Api-App-Id`); без него `[sources.superjob]` выключен.
- `GROQ_MODEL` — модель Groq (по умолчанию `openai/gpt-oss-120b`). Бесплатный тариф: 8K токенов в минуту, `max_completion_tokens` входит в счёт — потолок `GROQ_MAX_TOKENS`.

