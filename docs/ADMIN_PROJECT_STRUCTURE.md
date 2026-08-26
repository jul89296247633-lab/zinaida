# Зинаида — административная и техническая документация

_Обновлено: 2026-08-26. Для агента-преемника._

## Назначение и статус

Ежедневный новостной дайджест для Юлии: сбор трендов + RSS, ключевая фильтрация, отправка в Telegram. База — копия open-source проекта TrendRadar (upstream: sansan0/TrendRadar, GPL-3.0), настроенная под Юлию.

**Статус: в проде, проверено доставкой 2026-08-23.** Плановый запуск ежедневно 07:00 МСК (cron `0 4 * * *` UTC; GitHub может задержать старт на десятки минут — это норма).

История: 05.08.2026 исходный форк TrendRadar завис (GitHub не выдаёт таким форкам раннеры: запуски вечно `queued`, cancel/delete через API отдают 500/403 — лечится только пересозданием). 23.08.2026 конфигурация перенесена в этот **native-репозиторий** (не форк — раннеры работают как у обычных репо), форк удалён. В тот же день шаблоны сообщений переведены на русский, доставка верифицирована (3/3 батча успешно).

## Ключевые файлы

| Путь | Что там |
|---|---|
| `.github/workflows/crawler.yml` | Workflow «Зинаида»: cron, шаги, секреты. Trial-механика апстрима (7-дневный лимит + Check In) удалена; concurrency-группа `zinaida-*` |
| `config/config.yaml` | Источники: 3 платформы + 15 RSS-лент; режим отчёта `current`; таймзона Europe/Moscow; расписание (schedule) выключено. Standalone-регион: rss_feeds=[github-trending], max_items=4 |
| `config/frequency_words.txt` | 8 групп ключевых слов + глобальный фильтр (казино/беттинг). Синтаксис: `/regex/ => Имя группы`, движок — Python `re` с `IGNORECASE` |
| `trendradar/notification/splitter.py` | Основная сборка Telegram-сообщения — русские шаблоны (переведено 23.08) |
| `trendradar/notification/renderer.py`, `trendradar/report/formatter.py`, `trendradar/__main__.py` | Остальные шаблоны/метки режимов — тоже русские |
| `docs/` (assets, index.html) | Сайт апстрима — не трогать |

## Секреты и окружение

- `TELEGRAM_BOT_TOKEN` — токен бота **@TrendsJulbot** (значение: GitHub Secrets; копия у Юлии в её локальной папке ключей).
- `TELEGRAM_CHAT_ID` — 206754542 (личный чат Юлии с ботом).
- AI-функции (анализ/перевод) выключены — AI_API_KEY не задан, фильтр ключевым методом.

## Команды: проверка, прогон, деплой

Изменения попадают в прод простым push в `main` — отдельного деплоя нет (GitHub Actions).

1. **Локальный тест рендера** (без установки зависимостей проекта; нужен `pip install pytz`):
   ```bash
   python - <<'PY'
   import sys, types
   sys.path.insert(0, ".")
   m = types.ModuleType("litellm"); m.completion = lambda **k: None
   sys.modules["litellm"] = m
   from trendradar.notification.splitter import split_content_into_batches
   # report_data/rss_items см. git-историю этого документа или тренар ниже
   PY
   ```
   Требуемые поля title: `title, source_name, time_display, count, url, mobile_url, is_new, ranks, rank_threshold`; RSS-статы — `{word, count, titles}`.
2. **Синтаксис после правок**: `python -m py_compile <файлы>`.
3. **Боевой прогон**: `gh workflow run crawler.yml -R jul89296247633-lab/zinaida --ref main`, затем в логах искать `批次发送成功` (успех батчей Telegram; логи самого crawler'а китайские — это нормально, их видит только GitHub).
4. **Валидация YAML**: `python -c "import yaml; yaml.safe_load(open('...'))"`.

## Газетный формат и проекты дня (добавлено 26.08)

- Цепочка выжимок: парсер RSS хранит `summary` (≤500 симв.) → `core/analyzer.py` кладёт его в title_data → `report/formatter.py::clean_excerpt` чистит HTML и режет ~220 симв. → telegram-ветка `format_title_for_platform` добавляет строку `<i>выжимка</i>` под заголовком; в standalone — жирное имя, выжимка, `🔗 ссылка`, время.
- «🛠 Проекты дня» = standalone-регион: лента github-trending (mshibanami GitHubTrendingRSS daily/all.xml), 4 топ-репозитория без ключевого фильтра.
- Ленты из `standalone.rss_feeds` исключены из ключевой фильтрации (`_stats_filter` в `__main__.py`) — иначе проекты дублируются и дайджест раздувается (было 14 батчей, стало ~5).

## Дежурные операции

- **Сменить время**: `crawler.yml` → cron `М Ч * * *` в UTC (МСК = UTC+3).
- **Добавить RSS**: `config.yaml` → `rss.feeds`; сначала проверить `curl -sL -o /dev/null -w "%{http_code}"` (200) и что парсится. Проверено несовместимое: Reddit (403 для IP GitHub Actions), MarkTechPost (битый XML).
- **Править ключевые слова**: `frequency_words.txt`; помнить: `\b` в Python-регэкспах юникодный (работает для кириллицы), регистр не важен (IGNORECASE).
- **Новый источник умер**: не страшно, crawler пропускает упавшие ленты; заменить при случае.

## Риски и мониторинг

- **60 дней без активности репо** → GitHub сам выключает schedule. Симптом: нет дайджеста. Лечение: `gh api -X PUT repos/jul89296247633-lab/zinaida/actions/workflows/<id>/enable` или любой push.
- **Режим `current` без сохранения состояния**: между запусками SQLite в `output/` не сохраняется (workflow не коммитит состояние). «Только новое» (incremental) использовать нельзя — будет дублирование.
- Обновления upstream не подтягиваются (это копия, не форк). При желании — сверять вручную.
- Мониторинга нет; индикатор здоровья — ежедневное сообщение у Юлии в 09:00–09:05 МСК.

## Открытые вопросы / TBD

- Рассмотреть перенос на Docker/VPS (у Юлии есть Beget VPS 85.198.64.210) — рекомендация апстрима для долгой жизни; сейчас не требуется.
- При появлении 2+ получателей — вынести chat_id в переменные/список.
