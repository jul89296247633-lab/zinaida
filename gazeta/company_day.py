# -*- coding: utf-8 -*-
"""Сводка по компании за день: люди и регионы, сравнение с позавчера и с тем
же днём недели на прошлой неделе + недельный региональный срез, когда он есть.

Данные (Supabase id.tools, только чтение):
  employee_snapshots  — ежедневные срезы сотрудников по городам/юнитам;
  weekly_facts        — недельные факты регионов (появляются после сбора
                        «Еженедельного отчёта регионов»; пусто — блок молча
                        пропускается).

Доставка: автономно в Telegram (--send) теми же секретами, что у газеты;
в воркфлоу газеты шаг стоит перед выпуском, ошибки шага не роняют выпуск.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "company-summary.json"
OUT_MD = ROOT / "company-summary.md"

SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def log(msg):
    print(f"[зинаида-компания] {msg}", flush=True)


def rest(path, params=""):
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{path}?{params}",
        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "[]")


def rest_all(path, params):
    out = []
    while True:
        page = rest(path, f"{params}&limit=1000&offset={len(out)}")
        out += page
        if not page or len(page) < 1000:
            return out


def norm_unit(name):
    # города приходят с хвостом времени («Астрахань 18:41 (+1ч)») — обрезаем
    return re.sub(r"\s*\d{1,2}:\d{2}.*$", "", (name or "").strip()) or "?"


def snapshot(date):
    try:
        rows = rest_all(
            "employee_snapshots",
            f"select=external_id,full_name,position_name,org_unit&snapshot_date=eq.{date}",
        )
        return {r["external_id"]: r for r in rows if r.get("external_id")}
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log(f"срез {date} недоступен: {e}")
        return None


def fmt(n):
    return f"{n:,.0f}".replace(",", " ")


def pct(a, b):
    if not b:
        return None
    return (a - b) / b * 100


def people_block(d1, d2, d8, label1, label2, label8):
    """Люди: компания + движения + отклонения регионов."""
    lines = []
    total1, total2, total8 = len(d1), len(d2 or {}), len(d8 or {})
    if not total1:
        return None, "срез за вчера не собрался — блок людей пропущен"
    lines.append(f"Люди: {fmt(total1)}")
    if total2:
        delta = total1 - total2
        lines[-1] += f" · {delta:+d} к {label2}"
    if total8:
        delta = total1 - total8
        lines[-1] += f" · {delta:+d} к {label8} ({WEEKDAYS[dt.date.fromisoformat(label1).weekday()]})"

    by_unit = {d: defaultdict(set) for d in (1, 2, 8)}
    data = {1: d1, 2: d2, 8: d8}
    for i, snap in data.items():
        if not snap:
            continue
        for ext, row in snap.items():
            by_unit[i][norm_unit(row.get("org_unit"))].add(ext)

    moved = []
    if d2:
        hired = sorted(set(d1) - set(d2))
        gone = sorted(set(d2) - set(d1))
        for ext in hired[:6]:
            r = d1[ext]
            moved.append(f"+ {(r.get('full_name') or '?').strip()} ({norm_unit(r.get('org_unit'))}, {(r.get('position_name') or '?').strip()[:30]})")
        for ext in gone[:6]:
            r = d2[ext]
            moved.append(f"− {(r.get('full_name') or '?').strip()} ({norm_unit(r.get('org_unit'))})")
        if len(hired) > 6 or len(gone) > 6:
            moved.append(f"… и ещё {len(hired) - 6 + max(0, len(gone) - 6)} чел.")
    if moved:
        lines.append("Движения за день: " + " · ".join(moved))

    flags = []
    for unit in sorted(set(by_unit[1]) | set(by_unit[2]) | set(by_unit[8])):
        n1 = len(by_unit[1].get(unit, ()))
        n2 = len(by_unit[2].get(unit, ())) if d2 else None
        n8 = len(by_unit[8].get(unit, ())) if d8 else None
        if n1 < 3:
            continue  # малые юниты шумят
        if n2 is not None and abs(n1 - n2) >= 2:
            flags.append((unit, n1 - n2, "день", n1))
        elif n8 is not None and abs(n1 - n8) >= 3:
            flags.append((unit, n1 - n8, "неделя", n1))
    flags.sort(key=lambda x: -abs(x[1]))
    if flags:
        shown = " · ".join(f"{'↑' if d > 0 else '↓'} {u} {d:+d} (было {n})" for u, d, _, n in flags[:5])
        lines.append(f"Отклонения регионов: {shown}")
    return "\n".join(lines), None


def weekly_block():
    """Последний период с фактами: компания WoW по ключевым метрикам."""
    try:
        periods = rest(
            "weekly_periods",
            "select=id,date_from,date_to&order=created_at.desc&limit=12",
        )
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return None
    filled = []
    for p in periods:
        try:
            one = rest("weekly_facts", f"select=metric_key&period_id=eq.{p['id']}&limit=1")
        except (urllib.error.URLError, urllib.error.HTTPError):
            return None
        if one:
            filled.append(p)
        if len(filled) == 2:
            break
    if not filled:
        return None
    cur, prev = filled[0], (filled[1] if len(filled) > 1 else None)

    def totals(period):
        rows = rest_all(
            "weekly_facts",
            f"select=org_unit_id,metric_key,value&period_id=eq.{period['id']}"
            f"&comparison_year=eq.{period['date_from'][:4]}",
        )
        agg = defaultdict(float)
        for r in rows:
            agg[r["metric_key"]] += r["value"] or 0
        return agg

    cur_agg = totals(cur)
    if not cur_agg:
        return None
    lines = [f"Неделя {cur['date_from']}–{cur['date_to']}:"]
    key_metrics = [("rev_total", "выручка"), ("units_total", "штуки"), ("chairs", "кресла"), ("checks", "чеки")]
    shown = []
    for key, title in key_metrics:
        if key in cur_agg:
            shown.append(f"{title} {fmt(cur_agg[key])}")
    if shown:
        lines.append("· " + " · ".join(shown))
    if prev:
        prev_agg = totals(prev)
        wow = []
        for key, title in key_metrics:
            if key in cur_agg and key in prev_agg and prev_agg[key]:
                p = pct(cur_agg[key], prev_agg[key])
                if p is not None:
                    wow.append(f"{title} {p:+.1f}%")
        if wow:
            lines.append(f"· к неделе {prev['date_from']}–{prev['date_to']}: " + " · ".join(wow))
    return "\n".join(lines)


def build(day):
    d1_iso, d2_iso, d8_iso = day.isoformat(), (day - dt.timedelta(days=1)).isoformat(), (day - dt.timedelta(days=7)).isoformat()
    d1, d2, d8 = snapshot(d1_iso), snapshot(d2_iso), snapshot(d8_iso)
    if d1 is None:
        return None, "нет свежего среза сотрудников — сводка пропущена"
    people, warn = people_block(d1, d2, d8, d1_iso, d2_iso, d8_iso)
    weekly = weekly_block()
    parts = [f"Компания за {day.strftime('%d.%m.%Y')} ({WEEKDAYS[day.weekday()]})"]
    if people:
        parts.append(people)
    if weekly:
        parts.append(weekly)
    if warn:
        parts.append(f"внимание: {warn}")
    parts.append("источник: employee_snapshots (день)" + (", weekly_facts (неделя)" if weekly else ""))
    return "\n".join(parts), None


def send(text):
    import requests

    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text[:4000]},
        timeout=60,
    )
    r.raise_for_status()
    log("сводка компании отправлена в Telegram")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--days-back", type=int, default=1, help="0=сегодня, 1=вчера (по умолчанию)")
    args = ap.parse_args()
    if not (SB_URL and SB_KEY):
        print("нет SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY — сводка компании пропущена", flush=True)
        return 0

    day = dt.datetime.now(MSK).date() - dt.timedelta(days=args.days_back)
    text, err = build(day)
    if err:
        print(err, flush=True)
        return 0
    print(text, flush=True)
    OUT_MD.write_text(text, encoding="utf8")
    OUT_JSON.write_text(json.dumps({"day": day.isoformat(), "text": text}, ensure_ascii=False), encoding="utf8")
    if args.send and TG_TOKEN and TG_CHAT:
        send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
