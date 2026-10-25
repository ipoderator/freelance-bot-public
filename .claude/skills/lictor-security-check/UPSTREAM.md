# Источник: Lictor Security Check

`SKILL.md` в этой папке — **изменённая** версия навыка `lictor-security-check` из набора
Lictor AI.

- Источник: https://github.com/Raffa-jarrl/Lictor-AI,
  `skills/plugins/lictor-security-suite/skills/lictor-security-check/`, коммит
  `cfe8c00130ea972b66af23af7042bc8f503b4092` (2026-07-08), плагин 0.1.0.
- Лицензия: Apache-2.0, текст — `LICENSE` рядом. NOTICE автора: «Lictor AI, Copyright
  2026 Lictor AI. This product includes software developed at Lictor AI
  (https://lictor-ai.com).»

Что изменено (Apache-2.0, п. 4b): вместо общего аудита веб- и мобильных приложений на 48
проверок — 12 проверок, которые относятся к этому боту (секреты бота и бирж, вырезание из
логов, недоверенный текст заказов и резюме, HTML Telegram, запрет записи на биржи, анкета
публичного бота, публичный репозиторий). Убраны проверки для веба, мобильных приложений,
RAG и векторных баз, `checks/`, шаблон отчёта и ссылки на платный сервис. Текст на
русском, команды — под этот репозиторий.
