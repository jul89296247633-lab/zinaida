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

# ── выручка дня: orders.borboza display-mix-sells (даты в URL) ──────────────
# Полный набор параметров обязателен: без lastMonthSelect/exactMonth/location
# портал молча отдаёт нули. Числа в тыс. руб.; строка «Город (N)» несёт итог
# города, вложенные строки — точки (не суммировать вместе с итогом).
MIX_URL = "https://orders.borboza.com/stat/global-sale-points/display-mix-sells/"
MIX_PARAMS = "lastMonthSelect=0&exactMonth=-1&location=allRegions&displayData=displayData"
CITY_ROW_RX = re.compile(r"^(.+?)\s+\((\d+)\)$")


def fetch_days_money(dates):
    """Выручка по дням одной сессией: {iso: {города, маркетплейсы, дилеры}}}.
    Один вход на все даты (регламент: <=2 попытки за прогон), паузы >=3 с."""
    import importlib.util
    import time

    spec = importlib.util.spec_from_file_location("hd_tasks", Path(__file__).resolve().parent / "hd_tasks.py")
    hd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hd)

    def parse(html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select("table")
        if len(tables) < 2:
            return None
        rows = tables[1].select("tr")
        cities, marketplaces, dealers = {}, 0.0, 0.0
        section = "дилеры"

        def num(cell):
            text = re.sub(r"[\s ]", "", cell.get_text(" ") or "")
            if re.fullmatch(r"-?[\d.,]+", text):
                return float(text.replace(",", "."))
            return None

        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            label = re.sub(r"\s+", " ", cells[0].get_text(" ")).strip()
            value = num(cells[1])
            if not label or value is None:
                continue
            m = CITY_ROW_RX.match(label)
            if m:
                city = m.group(1).strip()
                cities[city] = cities.get(city, 0.0) + value
                section = "city"
                continue
            low = label.lower()
            if section == "дилеры" and low.startswith("итого от дилеров"):
                dealers = value
                section = "маркетплейсы"
            elif section == "маркетплейсы":
                if low.startswith("итого"):
                    section = "cities"
                elif low.startswith("маркетплейс"):
                    marketplaces += value
        if not cities and not marketplaces:
            return None
        return {"cities": cities, "marketplaces": marketplaces, "dealers": dealers}

    out = {}
    pw, browser = hd.launch()
    try:
        ctx = browser.new_context(viewport={"width": 1680, "height": 1050})
        page = ctx.new_page()
        first_url = f"{MIX_URL}?from={dates[0]}&to={dates[0]}&{MIX_PARAMS}"
        page.goto(first_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        if not hd.logged_in(page):
            ok = False
            for _attempt in (1, 2):
                if hd.login(page, ctx, os.environ["BORBOZA_LOGIN"], os.environ["BORBOZA_PASSWORD"]):
                    ok = True
                    break
                time.sleep(20)
            if not ok:
                return out
        for iso in dates:
            url = f"{MIX_URL}?from={iso}&to={iso}&{MIX_PARAMS}"
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3500)
            for _ in range(20):
                if page.locator("table").count() > 1:
                    break
                page.wait_for_timeout(3000)
            out[iso] = parse(page.content())
            page.wait_for_timeout(500)
    finally:
        browser.close()
        pw.stop()
    return out


# ── товарные группы: orders.borboza regions-line (шт, лаг отгрузок ~1 день) ──
GOODS_URL = "https://orders.borboza.com/stat/global-sale-points-goods/regions-line/"
# подгруппа бренда — ТОЧНОЕ имя («Yamaguchi», «US Medica Линейка SPA»);
# «Косметика Yamaguchi» — уже категория, не подгруппа
BRAND_SUBGROUP_RX = re.compile(
    r"^(yamaguchi|us medica|panasonic|fujiiryoki|anatomico|jet|x-line|"
    r"axiom( 2 robotics)?|bestec|smart wall|hakuju)(\s+линейка.*)?$", re.I
)


def good_group(section):
    s = (section or "").strip()
    if BRAND_SUBGROUP_RX.match(s):
        return None  # подгруппа бренда — категорию не меняет
    s = s.lower()
    if "кресл" in s or "соф" in s or "качалк" in s:
        return "кресла"
    if "массаж" in s or "стол" in s and "массаж" in s or "стул" in s and "массаж" in s:
        if "кресл" in s:
            return "кресла"
        return "массаж"
    if any(k in s for k in ("дорожк", "тренаж", "фитнес", "велосипед", "спорт", "миостимулятор", "палк", "степпер", "виброплатформ", "эллипт")):
        return "фитнес"
    if any(k in s for k in ("косметолог", "косметик", "маски для лица", "красот", "фен", "прибор для лица")):
        return "бьюти"
    return "прочее"


def fetch_goods_groups(dates):
    """Шт по группам {дата: {группа: шт}} одной сессией regions-line."""
    import importlib.util
    import time

    spec = importlib.util.spec_from_file_location("hd_tasks", Path(__file__).resolve().parent / "hd_tasks.py")
    hd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hd)

    def parse(html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select("table")
        if len(tables) < 2:
            return None
        groups = defaultdict(float)
        section = ""

        def num(text):
            t = re.sub(r"[\s ]", "", text or "")
            return float(t.replace(",", ".")) if re.fullmatch(r"-?[\d.,]+", t) else None

        for row in tables[1].select("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            vals = [c.get_text(" ").strip() for c in cells]
            if not vals or not vals[0]:
                continue
            if vals[0].isdigit():
                # строка товара: категорию задаёт последняя не-брендовая секция
                g = good_group(section)
                if g:
                    for v in vals[2:]:
                        n = num(v)
                        if n is not None:
                            groups[g] += n
            elif vals[0] not in ("#",):
                if good_group(vals[0]) is not None:
                    section = vals[0]
        return dict(groups) or None

    out = {}
    pw, browser = hd.launch()
    try:
        ctx = browser.new_context(viewport={"width": 1680, "height": 1050})
        page = ctx.new_page()
        first = f"{GOODS_URL}?from={dates[0]}&to={dates[0]}&{MIX_PARAMS}"
        page.goto(first, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        if not hd.logged_in(page):
            ok = False
            for _attempt in (1, 2):
                if hd.login(page, ctx, os.environ["BORBOZA_LOGIN"], os.environ["BORBOZA_PASSWORD"]):
                    ok = True
                    break
                time.sleep(20)
            if not ok:
                return out
        for iso in dates:
            url = f"{GOODS_URL}?from={iso}&to={iso}&{MIX_PARAMS}"
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            # ждём данные: ненулевая ячейка вне колонок #/Товар (загрузка асинхронная)
            for _ in range(30):
                ready = page.evaluate(
                    """() => {
                      const t = [...document.querySelectorAll('table')][1];
                      if (!t) return false;
                      for (const tr of t.querySelectorAll('tr')) {
                        const tds = tr.querySelectorAll('td');
                        for (let i = 2; i < tds.length; i++) {
                          const v = (tds[i].innerText || '').replace(/[\s,]/g, '');
                          if (/^\d+$/.test(v) && parseInt(v, 10) > 0) return true;
                        }
                      }
                      return false;
                    }"""
                )
                if ready:
                    break
                page.wait_for_timeout(3000)
            out[iso] = parse(page.content())
            page.wait_for_timeout(1500)
    finally:
        browser.close()
        pw.stop()
    return out


def goods_block(d2, d3, d9, label3, label9):
    """Товарные группы за последний полный день (шт) с дельтами."""
    if not (os.environ.get("BORBOZA_LOGIN") and os.environ.get("BORBOZA_PASSWORD")):
        return None
    try:
        days = fetch_goods_groups([d2.isoformat(), d3.isoformat(), d9.isoformat()])
        g2 = days.get(d2.isoformat())
        if not g2:
            return None
        g3 = days.get(d3.isoformat()) or {}
        g9 = days.get(d9.isoformat()) or {}
    except Exception as e:
        log(f"товарные группы недоступны: {e}")
        return None
    order = ("бьюти", "кресла", "массаж", "фитнес", "прочее")
    parts = []
    for key in order:
        if key not in g2:
            continue
        v = g2[key]
        d_day = v - g3.get(key, 0.0)
        d_week = v - g9.get(key, 0.0)
        parts.append(f"{key} {v:+.0f} ({d_day:+.0f}/{d_week:+.0f})")
    if not parts:
        return None
    lines = [
        f"Товары за {d2.strftime('%d.%m')} (шт; в скобках Δ к {label3} и к {label9}): "
        + " · ".join(parts),
    ]
    lines.append("· по отгрузкам, данные доходят с лагом ~1 день")
    return "\n".join(lines)


def money_block(d1, d2, d8, label2, label8):
    """Выручка за вчера против позавчера и того же дня недели (тыс. руб)."""
    if not (os.environ.get("BORBOZA_LOGIN") and os.environ.get("BORBOZA_PASSWORD")):
        return None
    try:
        days = fetch_days_money([d1.isoformat(), d2.isoformat(), d8.isoformat()])
        m1 = days.get(d1.isoformat())
        if m1 is None:
            return None
        m2 = days.get(d2.isoformat())
        m8 = days.get(d8.isoformat())
    except Exception as e:
        log(f"выручка дня недоступна: {e}")
        return None

    def tot(m):
        return (sum(m["cities"].values()) + m["marketplaces"]) if m else None

    t1, t2, t8 = tot(m1), tot(m2), tot(m8)
    lines = [f"Выручка за {d1.strftime('%d.%m')}: {fmt(t1)} тыс ₽"]
    if t2:
        d = t1 - t2
        lines[0] += f" · {d:+,.0f} к {label2} ({pct(t1, t2):+.1f}%)".replace(",", " ")
    if t8:
        d = t1 - t8
        lines[0] += f" · {d:+,.0f} к {label8} ({pct(t1, t8):+.1f}%)".replace(",", " ")
    top = sorted(m1["cities"].items(), key=lambda kv: -kv[1])[:4]
    if top:
        lines.append("· регионы: " + " · ".join(f"{c} {fmt(v)}" for c, v in top))
    lines.append(f"· маркетплейсы: {fmt(m1['marketplaces'])} тыс ₽")
    flags = []
    if m2:
        for city, v in m1["cities"].items():
            base = m2["cities"].get(city, 0.0)
            if base >= 100:
                dp = (v - base) / base * 100
                if dp >= 25:
                    flags.append(f"↑ {city} +{dp:.0f}%")
                elif dp <= -25:
                    flags.append(f"↓ {city} {dp:.0f}%")
    if m8:
        for city, v in m1["cities"].items():
            base = m8["cities"].get(city, 0.0)
            dp = (v - base) / base * 100 if base else 0
            if base >= 100 and abs(dp) >= 40 and not any(city in f for f in flags):
                flags.append(f"{'↑' if dp > 0 else '↓'} {city} к {label8} {dp:+.0f}%")
    if flags:
        lines.append("· отклонения: " + " · ".join(flags[:5]))
    return "\n".join(lines)

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
    money = money_block(day, day - dt.timedelta(days=1), day - dt.timedelta(days=7), d2_iso, d8_iso)
    goods = goods_block(day - dt.timedelta(days=1), day - dt.timedelta(days=2), day - dt.timedelta(days=8), (day - dt.timedelta(days=2)).isoformat(), (day - dt.timedelta(days=8)).isoformat())
    weekly = weekly_block()
    parts = [f"Компания за {day.strftime('%d.%m.%Y')} ({WEEKDAYS[day.weekday()]})"]
    if money:
        parts.append(money)
    if goods:
        parts.append(goods)
    if people:
        parts.append(people)
    if weekly:
        parts.append(weekly)
    if warn:
        parts.append(f"внимание: {warn}")
    parts.append("источник: display-mix-sells (выручка), regions-line (товары), employee_snapshots (люди)" + (", weekly_facts (неделя)" if weekly else ""))
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
