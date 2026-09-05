# -*- coding: utf-8 -*-
"""
Зинаида — генератор утренней газеты.

Поток: RSS-ленты (config/config.yaml) + GitHub Trending → сырьё →
редактор GLM (Z.ai) пишет выпуск по лекалу Юлии → markdown → HTML → PDF →
Telegram: PDF-документом + короткая обложка.

Флаги: --no-pdf --no-telegram (для локальной отладки).
"""
import argparse
import datetime as dt
import html as html_mod
import json
import os
import re
import sys
import time
import xml.etree.ElementTree  # noqa: F401  (feedparser тянет)

import feedparser
import requests
import yaml
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSK = ZoneInfo("Europe/Moscow")

FE_WINDOW_H = 36          # свежесть статьи, часов
CAP_PER_FEED = 10         # максимум статей на ленту в сырьё
CAP_GITHUB = 12           # сколько trending-репозиторий брать
EXCERPT = 340             # аннотация в сырьё, символов

ZAI_BASE = os.environ.get("ZAI_BASE_URL") or "https://api.z.ai/api/paas/v4"
# glm-5.x думает по 15+ минут и съедает лимит на рассуждениях; 4.7 с выключенным
# мышлением пишет сразу — проверено боевым судьёй дрилла в salesvoice (glm.ts).
ZAI_MODEL = os.environ.get("ZAI_MODEL") or "glm-4.7"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg):
    print(f"[зинаида] {msg}", flush=True)


# ── сбор сырья ────────────────────────────────────────────────────────────

def clean(text, limit=None):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    import html as h
    text = h.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut + "…"
    return text


def collect():
    with open(os.path.join(REPO, "config", "config.yaml"), encoding="utf8") as f:
        cfg = yaml.safe_load(f)
    feeds = cfg.get("rss", {}).get("feeds", [])
    now = dt.datetime.now(dt.timezone.utc)
    material = {}
    stats = []
    for feed in feeds:
        fid, name, url = feed.get("id"), feed.get("name", feed.get("id")), feed.get("url")
        if not url:
            continue
        cap = CAP_GITHUB if fid == "github-trending" else CAP_PER_FEED
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 ZinaidaDigest/1.0"})
            entries = parsed.entries or []
        except Exception as e:
            stats.append(f"{name}: ошибка {e}")
            continue
        items = []
        for e in entries:
            title = clean(e.get("title", ""))
            if not title:
                continue
            link = e.get("link", "")
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if e.get(key):
                    published = dt.datetime(*e[key][:6], tzinfo=dt.timezone.utc)
                    break
            if fid != "github-trending" and published:
                if (now - published).total_seconds() > FE_WINDOW_H * 3600:
                    continue
            summary = clean(e.get("summary", ""), EXCERPT)
            items.append({"title": title, "url": link,
                          "date": published.strftime("%d.%m %H:%M") if published else "",
                          "summary": summary})
            if len(items) >= cap:
                break
        if items:
            material[name] = items
            stats.append(f"{name}: {len(items)}")
    return material, stats


def raw_text(material):
    parts = []
    for name, items in material.items():
        parts.append(f"=== Лента: {name} ===")
        for it in items:
            line = f"- {it['title']}"
            if it["date"]:
                line += f" ({it['date']} МСК)"
            if it["summary"]:
                line += f" — {it['summary']}"
            if it["url"]:
                line += f" [источник: {it['url']}]"
            parts.append(line)
        parts.append("")
    return "\n".join(parts)


# ── редактор ──────────────────────────────────────────────────────────────

SYSTEM = """Ты — редактор личного утреннего дайджеста Юлии Рогачёвой (агентство «Четвёртый Форс»: внедрение ИИ, продажи, управление, автоматизация). Ты пишешь выпуск по структуре ниже и её голосом.

ГОЛОС: сильный практикующий руководитель. Спокойная экспертность, точность вместо громкости. Короткие абзацы, часто по одному предложению. Управленческая причинность: кто отвечает, что делает, сколько стоит, какой результат. Точные существительные, цепочки-формулы («данные → правила → AI → действие → контроль»). Интеллектуальная ирония без клоунады. Первое лицо где уместно («мне важно понимать», «я бы сделала так»).
СТОП-ЛИСТ: «секрет успеха», «выйти на новый уровень», «раскрыть потенциал», «важно отметить», «в современном мире», хайп «ИИ изменит всё», восклицательные знаки, эмодзи-спам, канцелярит, псевдоконкретика без основания, вода между тезисами.

ЧЕСТНОСТЬ: используй ТОЛЬКО факты, цифры и события из сырья. Цифры не выдумывать и не округлять «для красоты». Нет данных по теме — пропусти секцию или материал, не лей воду. Под каждым материалом — строку источника как в сырьё.

СТРУКТУРА ВЫПУСКА (markdown, заголовки секций точно в этом формате: `## NN · Название`):
## 01 · Тренды AI
3–5 новостей (мир + Россия). Формат материала:
### Заголовок-событие с главной цифрой или фактом
2–4 предложения сути: что произошло, цифры, почему важно.
*Источник: название из сырья*
> **ИДЕЯ КОНТЕНТА.** Конкретный формат (пост / рилс / карусель) с углом для аудитории Юлии: продажи, управление, внедрение ИИ. Одним-двумя предложениями.

## 02 · Модели: релизы, цены, скидки, сравнения
Всё о моделях ИИ: что вышло, кто сколько стоит, скидки и бесплатные тарифы, кто кого обгоняет в сравнениях и бенчмарках. Если в сырье есть цены/скидки/сравнения — обязательно вынеси. Формат материала как в секции 01 (заголовок → суть с цифрами → Источник → ИДЕЯ КОНТЕНТА).

## 03 · Маркетплейсы и ритейл
2–4 новости (RU + мир), формат тот же.

## 04 · SMM и контент
2–3 новости (платформы, кейсы, алгоритмы), формат тот же.

## 05 · Находки с GitHub
3–4 живых проекта из сырья. Формат:
### Название — что делает одним предложением
Что это, на чём написано, чем примечателен.
> **Зачем ей:** персонально для Юлии — где проект пригодится в консалтинге, продуктах или контенте. 1–2 предложения.
Ссылку на репозиторий приложи строкой *Источник: github.com/…*

## 06 · Приём дня: ИИ и автоматизация
Один практический рецепт, выросший из новостей дня. Формат: суть приёма → нумерованные шаги (3–5) → как проверить, что сработало.

## 07 · Вывод: о чём говорить сегодня
Синтез дня: одна главная тема, связывающая новости, и вывод для позиционирования «Четвёртого Форса».

## 08 · Для себя
Короткий личный блок НЕ для контента: забота о себе, тело, пауза, режим. 3–4 предложения, тёплый тон, без морализаторства.

ПРАВИЛА: суммарно 7–9 страниц текста не нужно — пиши плотно, каждый материал по делу. Не добавляй секции, которых нет в структуре. Не пиши вводных «сегодня в выпуске». Первый токен ответа — сразу `## 01`."""


def call_editor(material_text, today_label):
    user = f"""Дата выпуска: {today_label}.

СЫРЬЁ (RSS-ленты и GitHub Trending, собрано автоматически за последние {FE_WINDOW_H} часа):

{material_text}

Напиши выпуск дайджеста по структуре. Помни: только факты из сырья, цифры не выдумывать, пустые секции пропускать (кроме 01, 06, 07, 08 — они обязательны)."""
    key = os.environ["ZAI_API_KEY"]
    url = f"{ZAI_BASE}/chat/completions"
    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.6,
        "max_tokens": 12000,
        "stream": True,
    }
    if ZAI_MODEL.startswith("glm-4"):
        # гибридные 4.x: мышление выключается полностью (у 5.x нельзя)
        payload["thinking"] = {"type": "disabled"}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                              json=payload, stream=True, timeout=(30, 120))
            r.raise_for_status()
            parts, reasoning, usage = [], [], {}
            started, last_note = time.time(), 0.0
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                if delta.get("content"):
                    parts.append(delta["content"])
                if time.time() - last_note > 60:
                    log(f"…пишет: контент {sum(map(len, parts))} симв, "
                        f"размышления {sum(map(len, reasoning))} симв")
                    last_note = time.time()
            text = "".join(parts).strip()
            if text:
                log(f"редактор ответил: {len(text)} символов за {int(time.time() - started)} с, "
                    f"размышлений {sum(map(len, reasoning))} симв, "
                    f"токены: {usage.get('total_tokens', '?')}")
                return text
            joined = "".join(reasoning).strip()
            last_err = RuntimeError(
                f"контент пуст; размышлений {len(joined)} симв — вероятно, исчерпан max_tokens"
                if joined else "пустой ответ без размышлений")
        except Exception as e:
            last_err = e
            wait = 60 * (attempt + 1) if "429" in str(e) else 20 * (attempt + 1)
            log(f"попытка {attempt + 1} не удалась: {e}; пауза {wait} с")
            time.sleep(wait)
    raise RuntimeError(f"редактор не ответил: {last_err}")


# ── вёрстка ───────────────────────────────────────────────────────────────

def markdown_to_html(md_text):
    import markdown as md_lib
    return md_lib.markdown(md_text, extensions=["sane_lists", "smarty"])


def render_html(body_html, date_label, weekday):
    with open(os.path.join(REPO, "gazeta", "template.html"), encoding="utf8") as f:
        tpl = f.read()
    return (tpl.replace("{{DATE}}", html_mod.escape(date_label))
               .replace("{{WEEKDAY}}", html_mod.escape(weekday))
               .replace("{{BODY}}", body_html))


def build_pdf(html_text, out_path):
    from weasyprint import HTML
    HTML(string=html_text).write_pdf(out_path)
    return out_path


def section_titles(md_text):
    return [re.sub(r"^##\s*", "", line).strip() for line in md_text.splitlines()
            if line.startswith("## ")]


# ── идеи контента → контент-завод (интеграция, 2026-09-05) ─────────────────
# Секции 01–04 дайджеста содержат «> **ИДЕЯ КОНТЕНТА.** …» — вынимаем их
# вместе с заголовком/сутью/источником и POST-им в /api/ideas/ingest завода.
# Ошибки доставки НЕ роняют выпуск (теле-газета важнее): try/except + лог.

IDEA_SECTIONS = ("01", "02", "03", "04")   # тренды/модели/маркетплейсы/SMM
IDEA_SOURCE_ENV = os.environ.get("IDEAS_API_URL", "")
IDEA_SECRET_ENV = os.environ.get("IDEAS_INGEST_SECRET", "")


def iso_week(now):
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def extract_ideas(md_text, material):
    """Материалы секций 01–04 с блоком «ИДЕЯ КОНТЕНТА» → [{title, summary, url}]."""
    # FIFO-очереди ссылок по имени ленты (запасной url, если GLM не дал ссылку)
    feed_queues = {name: [it["url"] for it in items if it.get("url")]
                   for name, items in material.items()}

    items = []
    section = None
    cur = None

    def flush():
        nonlocal cur
        if not cur:
            return
        idea = cur.get("idea")
        if not idea:
            cur = None
            return
        summary = " ".join(cur.get("body", [])).strip()
        if summary:
            summary = summary[:500]
        summary = (f"Суть: {summary} → Идея: {idea[:500]}").strip()
        url = cur.get("url")
        if not url:
            # запасной путь: первая неиспользованная ссылка названной ленты
            src = (cur.get("source") or "").lower()
            for name, queue in list(feed_queues.items()):
                if name.lower() in src and queue:
                    url = queue.pop(0)
                    break
        items.append({"title": cur["title"][:300], "summary": summary, "url": url})
        cur = None

    for line in md_text.splitlines():
        h2 = re.match(r"^##\s+(\d\d)\s*·", line)
        if h2:
            flush()
            section = h2.group(1)
            continue
        if line.startswith("## "):
            flush()
            section = None
            continue
        h3 = line.startswith("### ")
        if h3:
            flush()
            if section in IDEA_SECTIONS:
                cur = {"title": re.sub(r"^###\s*", "", line).strip(),
                       "body": [], "idea": None, "source": None, "url": None}
            continue
        if not cur:
            continue
        m_src = re.match(r"^\*Источник:\s*(.+?)\*?\s*$", line.strip())
        if m_src:
            cur["source"] = m_src.group(1).strip()
            m_url = re.search(r"https?://\S+", m_src.group(1))
            if m_url:
                cur["url"] = m_url.group(0).rstrip(").,;*")
            continue
        m_idea = re.match(r"^>\s*\*\*ИДЕЯ КОНТЕНТА\.?\*\*\s*(.+)$", line.strip())
        if m_idea:
            cur["idea"] = m_idea.group(1).strip()
            continue
        if line.strip() and not line.strip().startswith(">"):
            cur["body"].append(line.strip())
    flush()
    return items


def push_ideas(now, md_text, material):
    """Собирает идеи из md дайджеста и POST-ит в завод. Ошибки не бросает."""
    if not (IDEA_SOURCE_ENV and IDEA_SECRET_ENV):
        log("идеи: env IDEAS_API_URL/IDEAS_INGEST_SECRET не заданы — пропуск")
        return
    try:
        ideas = extract_ideas(md_text, material)
        if not ideas:
            log("идеи: материалов с ИДЕЕЙ КОНТЕНТА нет — ничего не отправляем")
            return
        r = requests.post(
            IDEA_SOURCE_ENV,
            headers={"Authorization": f"Bearer {IDEA_SECRET_ENV}"},
            json={"week": iso_week(now), "source": "trends_weekly", "items": ideas},
            timeout=30,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.ok and data.get("ok"):
            log(f"идеи: отправлено {len(ideas)}, записано {data.get('inserted')}, "
                f"дублей {data.get('skipped')}")
        else:
            log(f"идеи: завод ответил {r.status_code}: {str(data)[:200]}")
    except Exception as e:
        log(f"идеи: не отправлены ({e}) — выпуск не пострадал")



# ── доставка ──────────────────────────────────────────────────────────────

def tg_api(method, **fields):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, data=fields, timeout=60)
    r.raise_for_status()
    return r.json()


def send_digest(pdf_path, cover_text):
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data={"chat_id": TG_CHAT, "caption": cover_text[:1000],
                  "parse_mode": "HTML"},
            files={"document": (os.path.basename(pdf_path), f, "application/pdf")},
            timeout=180,
        )
    r.raise_for_status()
    log("PDF отправлен")


def send_cover(text):
    tg_api("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML")
    log("обложка отправлена")


def notify_fail(err):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        tg_api("sendMessage", chat_id=TG_CHAT,
               text=f"Зинаида: выпуск не собрался, ошибка: {str(err)[:300]}")
    except Exception:
        pass


# ── main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--no-ingest", action="store_true",
                    help="не отправлять идеи в контент-завод (отладка)")
    args = ap.parse_args()

    now = dt.datetime.now(MSK)
    date_label = now.strftime("%d.%m.%Y")
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    weekday = weekdays[now.weekday()]
    log(f"выпуск от {date_label} ({weekday})")

    material, stats = collect()
    log("ленты: " + "; ".join(stats))
    if not material:
        raise RuntimeError("сырьё пустое — все ленты недоступны")

    md = call_editor(raw_text(material), f"{weekday}, {date_label}")
    md = re.sub(r"^```markdown\s*|\s*```$", "", md.strip())

    # Идеи контента → контент-завод (ошибки не роняют выпуск)
    if not args.no_ingest:
        push_ideas(now, md, material)

    titles = section_titles(md)
    body_html = markdown_to_html(md)
    html_text = render_html(body_html, date_label, weekday)
    out_html = os.path.join(REPO, "digest-preview.html")
    with open(out_html, "w", encoding="utf8") as f:
        f.write(html_text)

    pdf_path = None
    if not args.no_pdf:
        pdf_path = os.path.join(REPO, f"digest-{date_label.replace('.', '-')}.pdf")
        build_pdf(html_text, pdf_path)
        log(f"PDF готов: {pdf_path} ({os.path.getsize(pdf_path) // 1024} КБ)")

    if not args.no_telegram and pdf_path:
        cover = f"<b>Утренний дайджест · {weekday}, {date_label}</b>\n\n"
        cover += "\n".join(f"· {html_mod.escape(t)}" for t in titles)
        cover += "\n\nГазета в файле выше ⚆"
        send_digest(pdf_path, cover)

    log("готово")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        notify_fail(e)
        sys.exit(1)
