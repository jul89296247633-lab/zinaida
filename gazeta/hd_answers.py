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
FRESH_H = 72              # последнее сообщение не старше N часов
MAX_PER_RUN = int(os.environ.get("HD_ANSWER_MAX", "4"))
MAX_MSG_CHARS = 900       # сообщение ветки — сколько брать в промпт
TG_TOKEN = hd_tasks.TG_TOKEN
TG_CHAT = hd_tasks.TG_CHAT

ZAI_BASE = os.environ.get("ZAI_BASE_URL") or "https://api.z.ai/api/paas/v4"
ZAI_MODEL = os.environ.get("ZAI_MODEL") or "glm-4.7"


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


def draft_answer(task, rules):
    import requests

    key = os.environ["ZAI_API_KEY"]
    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_TMPL.format(rules=rules)},
            {"role": "user", "content": (
                f"ВЕТКА ЗАДАЧИ #{task['id']} «{hd_tasks.norm(task.get('title'))}»\n\n"
                f"{thread_text(task)}\n\nНапиши черновик ответа."
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

def send_draft(task, answer):
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
            answer = draft_answer(t, rules)
            if not (TG_TOKEN and TG_CHAT):
                log(f"#{t['id']} черновик (Telegram не задан): {answer[:200]}")
            else:
                send_draft(t, answer)
                log(f"#{t['id']} черновик отправлен")
            state["suggested"][t["id"]] = now.isoformat()
            sent += 1
        except Exception as e:
            log(f"#{t['id']} не обработана: {e} — повторимся в следующий час")
    save_state(state, now)
    log(f"готово: отправлено {sent} из {len(cand)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
