# -*- coding: utf-8 -*-
"""Ежедневная сводка по задачам hd.borboza.com — «задачи дня» для газеты.

Вход по регламенту автологина Borboza (скилл borboza-autologin): логин/пароль →
«Запросить код» → ждать видимую «Войти» → «Войти» с пустым кодом. Максимум
2 попытки за прогон, пауза 20 с, «Отправить смс повторно» не нажимается,
паузы >=3 с между обращениями к порталу.

Что делает:
  1) читает первые страницы «Входящих» (сортировка по последнему сообщению);
  2) отбирает задачи с активностью за целевой день (вчера по МСК);
  3) строит сводку: новые/закрытые/статусы/темы/согласования и отказы;
  4) пишет hd-summary.json + печатает markdown-блок для газеты.

Запуск автономный: python gazeta/hd_tasks.py [--send] — с --send сам отправляет
сводку в Telegram (те же секреты, что у газеты). gazeta/main.py читает
hd-summary.json и прикладывает блок к выпуску; ошибки этого модуля никогда
не роняют выпуск газеты.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "hd-summary.json"
BASE = "https://hd.borboza.com/tasks/incoming/"
PAGES = int(os.environ.get("HD_TASKS_PAGES", "3"))  # 20 задач на страницу
PAUSE = 3.0
MAX_LOGIN_ATTEMPTS = 2
LOGIN_RETRY_PAUSE = 20.0

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

TOPICS = [
    ("возвраты", r"возврат|вернуть товар|вплект"),
    ("скидки/цены", r"скидк|промокод|купон|снизить цену|пересчитать цену"),
    ("доставка/логистика", r"доставк|перевозк|отгрузк|перемещение товара|самовывоз"),
    ("оплаты", r"оплат|эквайринг|платежк|рассрочк|возврат средств|аванс"),
    ("качество/гарантия", r"брак|дефект|поврежд|гаранти|ремонт|не работает"),
    ("HR", r"vacanc|вакан|резюме|кандидат|стажёр|стажер|отпуск|табель|увольнени|преми|бонус"),
    ("реклама", r"реклам|директ|метрик"),
    ("маркетплейсы", r"маркетплейс|wildberries|озон|wb\b|ozon|яндекс маркет"),
    ("магазины", r"магазин|салон|витрин|инвентариз|ревизия"),
    ("IT/1С", r"\b1с\b|1c|сервисс|сервер|доступ|парол|штрихкод"),
    ("юридические", r"юрист|суд|иск|претензи|договор"),
]
ALLOW_RX = re.compile(r"согласова|одобр[еи]|оформля|оформим|верн[её]м|подтвержд", re.I)
DENY_RX = re.compile(r"не согласова|нельзя|отказ|отклон[еи]|запрещ|возврат невозможен|делаем отказ|не положено|не предусмотрен", re.I)


def log(msg):
    print(f"[зинаида-hd] {msg}", flush=True)


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_dt(s):
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", s or "")
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    return dt.datetime(y, mo, d, h, mi, tzinfo=MSK)


def topic_of(text):
    t = text.lower()
    for name, pat in TOPICS:
        if re.search(pat, t):
            return name
    return "прочее"


def status_from_events(events):
    for e in reversed(events or []):
        m = re.search(r'изменил статус задачи №\d+ с "[^"]*" на "([^"]+)"', e or "")
        if m:
            return m.group(1)
    return "Новый"


EXTRACT_JS = """() => {
  const tasks = [];
  document.querySelectorAll('span[id^="taskTitleSpan_"]').forEach((t) => {
    const id = t.id.replace('taskTitleSpan_', '');
    const title = (t.textContent || '').trim();
    const right = t.closest('td');
    if (!right) return;
    const row = right.closest('tr');
    const left = row ? row.querySelector('td') : null;
    const leftText = left ? left.innerText.replace(/\\u00a0/g, ' ') : '';
    const cont = right.querySelector('.task_messages_container');
    const msgs = [];
    const events = [];
    if (cont) {
      cont.querySelectorAll(':scope > div').forEach((d) => {
        let isMsg = false;
        d.querySelectorAll('img').forEach((im) => {
          if ((im.getAttribute('src') || '').includes('message.png')) isMsg = true;
        });
        const txt = (d.innerText || '').replace(/\\u00a0/g, ' ').trim();
        if (!txt) return;
        if (isMsg) {
          const authorEl = d.querySelector('span[style*="DF6195"], span[style*="df6195"]');
          const dtEl = d.querySelector('span.grayfont');
          let body = txt;
          if (authorEl) body = body.replace((authorEl.textContent || '').trim(), '');
          if (dtEl) body = body.replace((dtEl.textContent || '').trim(), '');
          msgs.push({
            author: authorEl ? authorEl.textContent.trim() : '',
            dt: dtEl ? dtEl.textContent.trim().replace(/^\\[|\\]$/g, '') : '',
            text: body.replace(/^[\\s\\r\\n]+/, '').trim(),
          });
        } else {
          events.push(txt.replace(/\\s+/g, ' ').trim());
        }
      });
    }
    tasks.push({ id, title, left: leftText, msgs, events });
  });
  return tasks;
}"""


def launch():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    # локальный обход: свежий playwright просит браузер, которого нет в кэше;
    # переменной можно указать полный путь к chrome.exe (на Actions не нужна)
    exe = os.environ.get("HD_CHROMIUM_PATH") or None
    browser = pw.chromium.launch(headless=True, executable_path=exe, args=["--no-sandbox"])
    return pw, browser


def first_visible(page, selector):
    loc = page.locator(selector)
    for i in range(loc.count()):
        item = loc.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            pass
    return None


def logged_in(page):
    if re.search(r"passport/login", page.url):
        return False
    return first_visible(page, 'input[type=password]') is None


def login(page, ctx, username, password):
    import time

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        log(f"вход: попытка {attempt}")
        pwd = first_visible(page, 'input[type=password]')
        if pwd is None:
            return logged_in(page)
        login_f = (
            first_visible(page, "input[name*=login i]")
            or first_visible(page, "input[name*=user i]")
            or first_visible(page, "input[type=text]")
        )
        login_f.fill(username)
        pwd.fill(password)
        page.wait_for_timeout(500)
        step1 = first_visible(page, "#smsCode") or first_visible(page, "button:has-text('Запросить код')")
        if step1:
            step1.click()
            log("нажата «Запросить код»")
        deadline = time.time() + 30
        enter = None
        while time.time() < deadline and enter is None:
            enter = first_visible(page, "#enterButton")
            if enter is None:
                page.wait_for_timeout(1000)
        if enter is None:
            log("кнопка «Войти» не появилась")
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(LOGIN_RETRY_PAUSE)
                page.goto("https://hd.borboza.com/", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(PAUSE * 1000)
            continue
        page.wait_for_timeout(5000)  # поле кода остаётся ПУСТЫМ
        enter.click()
        log("нажата «Войти» с пустым кодом")
        deadline = time.time() + 30
        while time.time() < deadline:
            if logged_in(page):
                return True
            page.wait_for_timeout(2000)
        if attempt < MAX_LOGIN_ATTEMPTS:
            time.sleep(LOGIN_RETRY_PAUSE)
    return False


def collect(username, password, day_from):
    pw, browser = launch()
    data = []
    try:
        ctx = browser.new_context(viewport={"width": 1680, "height": 1050})
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(PAUSE * 1000)
        if not login(page, ctx, username, password):
            raise RuntimeError("вход в hd.borboza.com не состоялся")
        log("вход выполнен")
        for p in range(1, PAGES + 1):
            if p > 1:
                page.goto(f"{BASE}?p={p}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(PAUSE * 1000)
            tasks = page.evaluate(EXTRACT_JS)
            data.extend(tasks)
            log(f"стр.{p}: задач {len(tasks)}")
            # ранний выход: сортировка по последнему сообщению — как только
            # все сообщения страницы старше целевого дня, дальше можно не идти
            last = [m.get("dt") for t in tasks for m in t.get("msgs", []) if m.get("dt")]
            if last and all((parse_dt(x) or day_from) < day_from for x in last):
                break
    finally:
        browser.close()
        pw.stop()
    return data


def build_summary(tasks, day_from, day_to):
    opened, closed, active, touched = [], [], [], []
    for t in tasks:
        st = status_from_events(t.get("events"))
        rec = {
            "id": t["id"],
            "title": norm(t.get("title"))[:120],
            "status": st,
            "topic": topic_of((t.get("title") or "") + " " + " ".join(m.get("text", "") for m in (t.get("msgs") or [])[:2])),
        }
        created = None
        for e in t.get("events") or []:
            m = re.search(r"\[(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\]\s+\S+\s+создал задачу", e)
            if m:
                created = parse_dt(m.group(1))
        if created and day_from <= created < day_to:
            opened.append(rec)
        for e in t.get("events") or []:
            m = re.search(r'\[(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\][^\[]*изменил статус[^\]]* на "(Выполнен|Выполнен и проверен|Отменено|Закрыл, не проверив)"', e)
            if m and day_from <= parse_dt(m.group(1)) < day_to:
                closed.append(dict(rec, status=m.group(2)))
                break
        if st in ("Новый", "В работе", "Ожидание действий третьей стороны"):
            active.append(rec)
        day_msgs = [m for m in (t.get("msgs") or []) if m.get("dt") and day_from <= (parse_dt(m["dt"]) or day_from - dt.timedelta(days=99)) < day_to]
        if day_msgs:
            touched.append(dict(rec, n_msgs=len(day_msgs)))

    approvals, denies, reversals = [], [], []
    for t in touched:
        for m in (x for x in tasks if x["id"] == t["id"]):
            day_hits = []
            for msg in m.get("msgs", []):
                d = parse_dt(msg.get("dt") or "")
                if not (d and day_from <= d < day_to):
                    continue
                text = norm(msg.get("text"))[:160]
                if DENY_RX.search(text):
                    denies.append(f"#{t['id']} {t['topic']}: {text}")
                    day_hits.append("deny")
                elif ALLOW_RX.search(text):
                    approvals.append(f"#{t['id']} {t['topic']}: {text}")
                    day_hits.append("allow")
            if "deny" in day_hits and "allow" in day_hits:
                # красный флаг из config/hd_rules.md: разворот решения за день
                reversals.append(f"#{t['id']} {t['topic']}: {t['title']}")

    return {
        "date": day_from.strftime("%d.%m.%Y"),
        "opened": opened,
        "closed": closed,
        "active_sample": active,
        "active_by_status": dict(Counter(a["status"] for a in active)),
        "touched": [t["id"] for t in touched],
        "topics_touched": dict(Counter(t["topic"] for t in touched)),
        "approvals": approvals,
        "denies": denies,
        "reversals": reversals,
    }


def summary_md(s):
    lines = [f"Задачи hd.borboza за {s['date']}:"]
    lines.append(f"· новые: {len(s['opened'])}, закрыты: {len(s['closed'])}, затронуто веток: {len(s['touched'])}")
    if s["active_by_status"]:
        act = ", ".join(f"{k} {v}" for k, v in s["active_by_status"].items())
        lines.append(f"· открытых в свежей ленте: {act}")
    if s["topics_touched"]:
        top = ", ".join(f"{k} {v}" for k, v in Counter(s["topics_touched"]).most_common(5))
        lines.append(f"· темы дня: {top}")
    if s["approvals"]:
        lines.append(f"· согласований: {len(s['approvals'])}")
    if s["denies"]:
        lines.append(f"· отказов: {len(s['denies'])}")
    for d in s["denies"][:3]:
        lines.append(f"  – {d}")
    for r in s.get("reversals", [])[:3]:
        lines.append(f"  ! разворот решения за день: {r}")
    return "\n".join(lines)


def send(text):
    import requests

    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=60,
    )
    r.raise_for_status()
    log("сводка отправлена в Telegram")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="отправить сводку в Telegram")
    ap.add_argument("--days-back", type=int, default=1)
    args = ap.parse_args()

    username = os.environ.get("BORBOZA_LOGIN")
    password = os.environ.get("BORBOZA_PASSWORD")
    if not (username and password):
        print("нет BORBOZA_LOGIN/BORBOZA_PASSWORD — сводка пропущена", flush=True)
        return 0

    now = dt.datetime.now(MSK)
    day_to = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_from = day_to - dt.timedelta(days=args.days_back)

    tasks = collect(username, password, day_from)
    s = build_summary(tasks, day_from, day_to)
    SUMMARY.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf8")
    md = summary_md(s)
    print(md, flush=True)
    (ROOT / "hd-summary.md").write_text(md, encoding="utf8")
    if args.send and TG_TOKEN and TG_CHAT:
        send(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
