# AGENTS.md — Зинаида

Правила для AI-агентов, работающих с этим репозиторием.

## Что это

Настроенная копия TrendRadar: ежедневный дайджест новостей для Юлии в Telegram (@TrendsJulbot). Прод = GitHub Actions из `main`, отдельного сервера нет.

## Правила работы

1. Читай в порядке: `AGENTS.md` → `docs/ADMIN_PROJECT_STRUCTURE.md` → `docs/USER_INSTRUCTIONS.md` → `config/` → код.
2. Секреты не коммитить и не печатать; значения живут в GitHub Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
3. После правок шаблонов/конфигурации: `python -m py_compile` + локальный рендер-тест splitter (рецепт в админ-доке) + боевой `workflow_dispatch` и проверка логов на `批次发送成功`.
4. Русские строки локализации уже внесены в `trendradar/notification/*`, `trendradar/report/formatter.py`, `trendradar/__main__.py` — правки делать точечно, не откатывать на китайские оригиналы. Логи crawler'а оставлены китайскими намеренно.
5. Режим отчёта не менять с `current` на `incremental`/`daily` без понимания, что состояние между запусками не сохраняется (см. админ-док).
6. Cron в `crawler.yml` — UTC (МСК = UTC+3).
7. Документацию хендоффа держать актуальной: `docs/USER_INSTRUCTIONS.md`, `docs/ADMIN_PROJECT_STRUCTURE.md`, `docs/PROJECT_LIBRARY.md` (правило Handoff Documentation Rule).

## Handoff Documentation Rule

При любой передаче проекта обновить все три документа выше и дату в них; внешняя библиотека диалогов Юлии (biblioteka-dialogov, Supabase `public.dialog_library`) обновляется при наличии write-доступа.
