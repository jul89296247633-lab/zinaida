# -*- coding: utf-8 -*-
"""Часовой агент ответов на задачи hd.borboza.com.

Раз в час (workflow hd-answers.yml): войти на портал → взять свежие
«Входящие» → отобрать задачи, которые ждут ответа (статус «Новый», последнее
сообщение от автора задачи, не старше FRESH_H часов, ответ ещё не предлагали)
→ для каждой попросить GLM написать черновик ответа по правилам
config/hd_rules.md → прислать черновик в Telegram-бота.

Агент НИКОГДА не пишет в Borboza — только черновики человеку. Повторной
отправки по той же задаче нет: обработанные id хранятся в
.hd-answer-state/state.json (кэш Actions).
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hd_tasks  # noqa: E402  (вход, сбор страниц, разбор)

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".hd-answer-state" / "state.json"
RULES_PATH = ROOT / "config" / "hd_rules.md"
AD_PRICES_PATH = ROOT / "config" / "hd_ad_prices.json"
FRESH_H = 72              # последнее сообщение не старше N часов
MAX_PER_RUN = int(os.environ.get("HD_ANSWER_MAX", "4"))
MAX_MSG_CHARS = 900       # сообщение ветки — сколько брать в промпт
TG_TOKEN = hd_tasks.TG_TOKEN
TG_CHAT = hd_tasks.TG_CHAT

ZAI_BASE = os.environ.get("ZAI_BASE_URL") or "https://api.z.ai/api/paas/v4"
ZAI_MODEL = os.environ.get("ZAI_MODEL") or "glm-4.7"

# ── анализ цены размещения рекламы (сравнение по регионам) ─────────────────

AD_DETECT_RX = re.compile(
    r"согласова\w*\s+[^.]{0,40}(реклам|размещени|баннер|банер|стелл|лифт|вывеск)"
    r"|(размещени\w*|изготовлени\w*)\s+[^.]{0,30}(баннер|банер|стелл|вывеск|лифт)"
    r"|реклам\w*\s+в\s+лифт|аудиореклам|реклама на радио|ТВ реклам|наружн\w* реклам",
    re.I,
)
CITIES = (
    "Москва|Санкт-Петербург|СПб|Казань|Набережные Челны|Нижнекамск|Красноярск|"
    "Архангельск|Барнаул|Бийск|Йошкар-Ола|Белгород|Мурманск|Астрахань|Воронеж|"
    "Тольятти|Самара|Саратов|Иркутск|Шелехово|Омск|Вологда|Череповец|Екатеринбург|"
    "Новосибирск|Тюмень|Сочи|Краснодар|Ростов[-а-я]*|Уфа|Пермь|Челябинск|"
    "Владивосток|Хабаровск|Кемерово|Томск|Курск|Липецк|Ставрополь|Магнитогорск|Сургут"
)
MONEY_RX = re.compile(r"(\d[\d\s.,]{2,9})\s*(?:руб|₽|р\.)", re.I)


def _num(s):
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s else None


def ad_placement_type(text):
    t = text.lower()
    if re.search(r"лифт", t):
        return "лифты"
    if re.search(r"аудиореклам|аудио\s+реклам|реклама?.{0,20}(тц|трц)", t):
        return "аудио ТЦ"
    if re.search(r"радио", t):
        return "радио"
    if re.search(r"\bтв\b|телевид|телеканал|эфир", t):
        return "ТВ"
    if re.search(r"баннер|банер|стелл|вывеск|фасад|входн\w* групп|наружн", t):
        return "наружка"
    if re.search(r"мероприяти|фестивал|выставк|интершарм", t):
        return "мероприятие"
    if re.search(r"журнал|авиа|аэрофлот|utair", t):
        return "пресса/авиа"
    return None


def ad_city(text):
    m = re.search(r"(?:г\.|г\s|городе\s|города\s)?(" + CITIES + r")", text, re.I)
    return m.group(1).strip() if m else None


def ad_period(text):
    t = text.lower()
    if re.search(r"\bгод\w*\b|на 202\d год", t):
        return "год"
    if re.search(r"\d?\s*мес", t):
        return "мес"
    if re.search(r"недел", t):
        return "нед"
    if re.search(r"бессрочн", t):
        return "бессрочно"
    return "разово"


PER_TO_MONTH = {"мес": 1, "нед": 4.3, "год": 1 / 12.0, "разово": 1, "бессрочно": 1}


def ad_price_analysis(text):
    """Сводка адекватности цены размещения: сравнение с другими регионами.

    Возвращает (блок_для_TG, вопросы_для_черновика) или None, если текст
    не похож на согласование размещения рекламы.
    """
    if not AD_DETECT_RX.search(text):
        return None
    ptype = ad_placement_type(text)
    if not ptype:
        return None
    city = ad_city(text)
    money = sorted({m for m in (_num(x) for x in MONEY_RX.findall(text)) if m and 1000 <= m <= 3000000})
    period = ad_period(text)
    deals = []
    if AD_PRICES_PATH.exists():
        try:
            deals = json.loads(AD_PRICES_PATH.read_text(encoding="utf8")).get("deals", [])
        except ValueError:
            pass

    comparables = [d for d in deals if d.get("type") == ptype and d.get("city") not in (None, city, "не указан")]
    if not comparables:
        comparables = [d for d in deals if d.get("type") == ptype]
    per_month = [d["price"] / PER_TO_MONTH.get(d.get("period", "разово"), 1) for d in comparables]
    per_month.sort()
    median = per_month[len(per_month) // 2] if per_month else None

    lines = [f"Размещение: {ptype}" + (f", {city}" if city else ", город не указан")]
    if money:
        shown = ", ".join(f"{m:,.0f}".replace(",", " ") for m in money[:5])
        lines.append(f"Цены в задаче: {shown} ({period})")
    if median:
        # по самому дешёвому размещению каждого города — охват регионов, а не объектов
        by_city = {}
        for d in comparables:
            c = d.get("city") or "не указан"
            if c not in by_city or d["price"] < by_city[c]["price"]:
                by_city[c] = d
        top = sorted(by_city.values(), key=lambda d: d["price"])[:4]
        cmp_txt = "; ".join(
            f"{d.get('city', '?')}: {d['price']:,.0f}".replace(",", " ")
            + f"/{d.get('period')}" + (f" ({d['object'][:30]}, #{d['task']})" if d.get("object") else "")
            for d in top
        )
        lines.append(f"Другие регионы ({ptype}): {cmp_txt}")
        lines.append(f"Медиана по базе: {median:,.0f} ₽/мес".replace(",", " "))
        main = money[-1] if money else None
        if main and main > median * PER_TO_MONTH.get(period, 1) * 1.3:
            lines.append("ВЫВОД: выше сопоставимых регионов — просить обоснование/скидку")
        elif main and main < median * PER_TO_MONTH.get(period, 1) * 0.7:
            lines.append("ВЫВОД: ниже медианы — цена выгодная")
        elif main:
            lines.append("ВЫВОД: в рынке по базе")
    else:
        lines.append("Сопоставимых размещений в базе нет — запрашиваем прайс и охваты")

    questions = []
    if not city:
        questions.append("укажите город/регион размещения")
    if period == "разово" and ptype in ("аудио ТЦ", "наружка", "радио"):
        questions.append("срок размещения: за месяц или разово?")
    if ptype == "аудио ТЦ":
        questions.append("частота выходов и хронометраж ролика")
    if ptype == "лифты":
        questions.append("сколько лифтов/домов, срок и входит ли печать и монтаж")
    if ptype == "наружка":
        questions.append("что входит в цену: печать, монтаж/демонтаж, автовышка, срок размещения")
    if ptype in ("аудио ТЦ", "наружка", "мероприятие"):
        questions.append("охват/трафик объекта")
    if median and money and money[-1] > median * PER_TO_MONTH.get(period, 1) * 1.3:
        for d in list(by_city.values())[:2]:
            questions.append(
                f"почему дороже: в городе {d.get('city')} {d['object'][:30]} стоил "
                f"{d['price']:,.0f} ₽/{d.get('period')}".replace(",", " ")
            )
        questions.append("есть ли вариант скидки или бартера")
    if not questions:
        questions.append("частота/срок и что входит в стоимость — для полноты заявки")
    return "\n".join(lines), questions


def log(msg):
    print(f"[зинаида-ответы] {msg}", flush=True)


# ── состояние (кэш Actions) ────────────────────────────────────────────────

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf8"))
        except ValueError:
            pass
    return {"suggested": {}}


def save_state(state, now):
    # забываем задачи старше 14 дней — кэш не растёт бесконечно
    cut = (now - dt.timedelta(days=14)).isoformat()
    state["suggested"] = {k: v for k, v in state["suggested"].items() if v > cut}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf8")


# ── отбор задач, ждущих ответа ──────────────────────────────────────────────

def clean_thread_text(s):
    s = re.sub(r"\+ (SMS|email)[^\n]*", " ", s)
    s = re.sub(r"(?:[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+[, ]+){3,}", " ", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def candidates(tasks, now, suggested):
    out = []
    for t in tasks:
        msgs = t.get("msgs") or []
        if not msgs or hd_tasks.status_from_events(t.get("events")) != "Новый":
            continue
        last = msgs[-1]
        last_dt = hd_tasks.parse_dt(last.get("dt") or "")
        if not last_dt or (now - last_dt).total_seconds() > FRESH_H * 3600:
            continue
        # ждём ответа: последнее сообщение от автора задачи (или оно единственное);
        # пустой автор = вердикт из интерфейса — уже отвечено
        first_author = (msgs[0].get("author") or "").strip()
        last_author = (last.get("author") or "").strip()
        if not last_author:
            continue
        if len(msgs) > 1 and last_author != first_author:
            continue
        if t["id"] in suggested:
            continue
        out.append(t)
    out.sort(key=lambda t: hd_tasks.parse_dt((t.get("msgs") or [{}])[-1].get("dt") or "") or now)
    return out[:MAX_PER_RUN]


def thread_text(t):
    left = clean_thread_text(re.sub(r"\s+", " ", t.get("left") or ""))[:300]
    parts = [f"ШАПКА: {left}"]
    for m in (t.get("msgs") or [])[-6:]:
        body = clean_thread_text(m.get("text") or "")[:MAX_MSG_CHARS]
        parts.append(f"{m.get('author') or '—'} [{m.get('dt')}]: {body}")
    return "\n".join(parts)


# ── черновик через GLM ──────────────────────────────────────────────────────

SYSTEM_TMPL = """Ты — Зинаида, помощница руководителя компании Yamaguchi. Тебе даны ПРАВИЛА ОТВЕТОВ компании (выжимка из реальных задач) и ВЕТКА задачи из внутренней системы hd.borboza.com. Напиши черновик ответа, который ответственный мог бы отправить в задачу.

Требования:
- Пиши от лица подходящей по правилам роли (юрист / сервис / бухгалтерия / руководитель / КЦ), коротко и по делу, в стиле компании.
- Если по правилам не хватает данных (заключение сервиса, акт тех. состояния, номер продажи, реквизиты, срок гарантии) — черновик просит именно их; это нормальный рабочий ответ.
- Решение (согласовано / отказ / сумма) предлагай только когда правил и данных ветки достаточно. Если решение за руководителем — так и напиши, что требуется согласование.
- Не выдумывай факты, номера, суммы и даты, которых нет в ветке и правилах.
- Объём: 2–5 предложений, готовых вставить в задачу.

ПРАВИЛА ОТВЕТОВ:
{rules}"""


def draft_answer(task, rules, ad_context=None):
    import requests

    key = os.environ["ZAI_API_KEY"]
    extra = ""
    if ad_context:
        block, questions = ad_context
        extra = (
            "\n\nЗАДАЧА НА СОГЛАСОВАНИЕ РАЗМЕЩЕНИЯ РЕКЛАМЫ. Ниже — АНАЛИЗ ЦЕНЫ по базе "
            "сопоставимых размещений компании. В черновике: кратко сошлись на цифры базы "
            "(другие регионы), задай вопросы из списка (все или почти все, естественно языком), "
            "и если цена выше сопоставимых — попроси обоснование, скидку или бартер. "
            "Финальное согласование не предлагай — его даёт руководитель.\n"
            f"АНАЛИЗ ЦЕНЫ:\n{block}\nВОПРОСЫ К ЗАЯВИТЕЛЮ:\n- " + "\n- ".join(questions)
        )
    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_TMPL.format(rules=rules)},
            {"role": "user", "content": (
                f"ВЕТКА ЗАДАЧИ #{task['id']} «{hd_tasks.norm(task.get('title'))}»\n\n"
                f"{thread_text(task)}{extra}\n\nНапиши черновик ответа."
            )},
        ],
        "temperature": 0.4,
        "max_tokens": 700,
    }
    if ZAI_MODEL.startswith("glm-4"):
        payload["thinking"] = {"type": "disabled"}
    r = requests.post(
        f"{ZAI_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=(30, 180),
    )
    r.raise_for_status()
    text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("пустой ответ модели")
    return re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()


# ── доставка ────────────────────────────────────────────────────────────────

def send_draft(task, answer, ad_block=None):
    import requests

    first = (task.get("msgs") or [{}])[0]
    last = (task.get("msgs") or [{}])[-1]
    lines = [
        "<b>Зинаида · черновик ответа</b>",
        html.escape(f"#{task['id']} {hd_tasks.norm(task.get('title'))}"),
        html.escape(f"От {first.get('author') or '—'}, {first.get('dt', '')}"),
    ]
    last_text = clean_thread_text(last.get("text") or "")[:220]
    if last_text:
        lines.append(html.escape(f"Последнее: {last_text}…"))
    if ad_block:
        lines.append("")
        lines.append("<b>Анализ цены размещения:</b>")
        lines.append(html.escape(ad_block))
    lines.append("")
    lines.append("Предлагаемый ответ:")
    lines.append(html.escape(answer))
    lines.append("")
    lines.append(f"https://hd.borboza.com/tasks/item/?task_id={task['id']}")
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": "\n".join(lines)[:4000], "parse_mode": "HTML"},
        timeout=60,
    )
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="без GLM и Telegram: показать кандидатов")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    username = os.environ.get("BORBOZA_LOGIN")
    password = os.environ.get("BORBOZA_PASSWORD")
    now = dt.datetime.now(hd_tasks.MSK)
    state = load_state()

    if not (username and password):
        print("нет BORBOZA_LOGIN/BORBOZA_PASSWORD — агент пропущен", flush=True)
        return 0

    tasks = hd_tasks.collect(username, password, now - dt.timedelta(hours=FRESH_H))
    cand = candidates(tasks, now, state["suggested"])
    log(f"задач прочитано {len(tasks)}, ждут ответа: {len(cand)}")
    if args.dry_run or not cand:
        for t in cand:
            log(f"кандидат #{t['id']} {hd_tasks.norm(t.get('title'))[:80]}")
        return 0

    rules = RULES_PATH.read_text(encoding="utf8") if RULES_PATH.exists() else "(правила не найдены)"
    sent = 0
    for t in cand:
        try:
            ad_context = ad_price_analysis(thread_text(t))
            answer = draft_answer(t, rules, ad_context=ad_context)
            ad_block = ad_context[0] if ad_context else None
            if not (TG_TOKEN and TG_CHAT):
                log(f"#{t['id']} черновик (Telegram не задан): {answer[:200]}")
            else:
                send_draft(t, answer, ad_block=ad_block)
                log(f"#{t['id']} черновик отправлен" + (" (+анализ цены)" if ad_block else ""))
            state["suggested"][t["id"]] = now.isoformat()
            sent += 1
        except Exception as e:
            log(f"#{t['id']} не обработана: {e} — повторимся в следующий час")
    save_state(state, now)
    log(f"готово: отправлено {sent} из {len(cand)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
