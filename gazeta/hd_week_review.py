# -*- coding: utf-8 -*-
"""Недельный обзор задач hd.borboza — локальный прогон (Яндекс-Браузер).

Собирает «Входящие» за последние 7 дней (это задачи с участием Юлии —
«Входящие» аккаунта и есть это множество), считает обзор и кладёт его в
ветку hd-inbox (week-review.json + .md); отправка в Telegram — недельный
воркфлоу на GitHub (ключи бота только там).

Блоки обзора: объём недели, темы, согласования/отказы, флаги «разворот
решения» (правила config/hd_rules.md) и ЛИЧНОЕ УЧАСТИЕ Юлии: адресованные
ей задачи, её вердикты, ждущие её решения.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hd_collect_local as local  # noqa: E402  (worktree/git helpers, env fallback)
import hd_tasks  # noqa: E402

WORKTREE = local.WORKTREE
OUT_DIR = "hd-inbox"
YOU = "Рогачева"
VERDICT_RX = re.compile(r"(согласовано|не согласовано|одобрено\s+\d+\s*%?)", re.I)


def log(msg):
    print(f"[hd-week] {msg}", flush=True)


def status_of(t):
    return hd_tasks.status_from_events(t.get("events"))


def topic_of(t):
    text = ((t.get("title") or "") + " " + " ".join(m.get("text", "") for m in (t.get("msgs") or [])[:2])).lower()
    for name, pat in hd_tasks.TOPICS:
        if re.search(pat, text):
            return name
    return "прочее"


def main():
    local.load_env_fallback()
    now = dt.datetime.now(hd_tasks.MSK)
    week_from = now - dt.timedelta(days=7)
    tasks = hd_tasks.collect(
        os.environ["BORBOZA_LOGIN"], os.environ["BORBOZA_PASSWORD"], week_from
    )
    log(f"задач прочитано: {len(tasks)}")

    opened, closed, touched, your_addressed, awaiting_you = [], [], [], [], []
    verdicts = Counter()
    approvals, denies, reversals = 0, 0, 0
    topics_week = Counter()

    def in_week(d):
        return d and week_from <= d < now

    for t in tasks:
        left = t.get("left") or ""
        msgs = t.get("msgs") or []
        st = status_of(t)
        addressed_to_you = re.search(r"Кому:\s*[^\n]*Рогачева", left) is not None
        dts = [hd_tasks.parse_dt(m.get("dt") or "") for m in msgs]
        week_msgs = [m for m, d in zip(msgs, dts) if in_week(d)]
        if not week_msgs and st not in ("Новый",):
            continue

        created = None
        for e in t.get("events") or []:
            m = re.search(r"\[(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\]\s+\S+\s+создал задачу", e)
            if m:
                created = hd_tasks.parse_dt(m.group(1))
                break
        if in_week(created):
            opened.append(t)
            topics_week[topic_of(t)] += 1
        closed_in_week = False
        for e in t.get("events") or []:
            m = re.search(
                r"\[(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\][^\[]*изменил статус[^\]]* на \"(Выполнен|Выполнен и проверен|Отменено|Закрыл, не проверив)\"",
                e,
            )
            if m and in_week(hd_tasks.parse_dt(m.group(1))):
                closed_in_week = True
                break
        if closed_in_week:
            closed.append(t)
        if week_msgs:
            touched.append(t)

        # личное участие
        if addressed_to_you:
            your_addressed.append(t)
            if st == "Новый":
                last_author = (msgs[-1].get("author") or "").strip() if msgs else ""
                if last_author and YOU not in last_author:
                    awaiting_you.append(t)

        day_hit_allow = day_hit_deny = False
        for m in week_msgs:
            author = (m.get("author") or "").strip()
            text = hd_tasks.norm(m.get("text") or "")
            is_yours = YOU in author or (not author and YOU in text[:40])
            if not is_yours:
                v = VERDICT_RX.search(text[:60])
                if v:
                    if "не соглас" in v.group(1).lower():
                        day_hit_deny = True
                    else:
                        day_hit_allow = True
                continue
            v = VERDICT_RX.search(text)
            if v:
                verdicts[v.group(1).lower().strip()] += 1
        if day_hit_allow:
            approvals += 1
        if day_hit_deny:
            denies += 1
        if day_hit_allow and day_hit_deny:
            reversals += 1

    open_now = [t for t in tasks if status_of(t) in ("Новый", "В работе", "Ожидание действий третьей стороны")]
    by_status = Counter(status_of(t) for t in open_now)

    lines = [
        f"Задачи за неделю {week_from:%d.%m}–{now:%d.%m}:",
        f"· новые: {len(opened)} · закрыты: {len(closed)} · движение в {len(touched)} ветках",
        f"· открыто сейчас: {len(open_now)} (" + ", ".join(f"{k} {v}" for k, v in by_status.most_common()) + ")",
    ]
    if topics_week:
        lines.append("· темы новых: " + " · ".join(f"{k} {v}" for k, v in topics_week.most_common(6)))
    lines.append(f"· согласований: {approvals} · отказов: {denies}" + (f" · ⚠ разворотов решения за неделю: {reversals}" if reversals else ""))
    lines.append(
        "Ваше участие: "
        f"адресовано вам: {len(your_addressed)} · ваши вердикты: "
        + (" · ".join(f"«{k}» {v}" for k, v in verdicts.most_common(4)) if verdicts else "—")
    )
    if awaiting_you:
        ids = ", ".join(f"#{t['id']}" for t in awaiting_you[:6])
        lines.append(f"· ждут вашего решения: {len(awaiting_you)} ({ids})")
    else:
        lines.append("· ждут вашего решения: 0")

    text = "\n".join(lines)
    print(text, flush=True)

    local.prepare_worktree()
    out = WORKTREE / OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(),
        "week_from": week_from.date().isoformat(),
        "text": text,
        "counts": {
            "opened": len(opened), "closed": len(closed), "touched": len(touched),
            "open_now": len(open_now), "your_addressed": len(your_addressed),
            "awaiting_you": len(awaiting_you), "approvals": approvals,
            "denies": denies, "reversals": reversals,
            "verdicts": dict(verdicts),
        },
    }
    (out / "week-review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf8")
    (out / "week-review.md").write_text(text, encoding="utf8")
    local.sh("git", "add", OUT_DIR, cwd=WORKTREE)
    if local.sh("git", "status", "--porcelain", cwd=WORKTREE):
        local.sh("git", "-c", "user.name=Zinaida Local Collector",
                 "-c", "user.email=zinaida-local@users.noreply.github.com",
                 "commit", "-m", f"week-review {now:%Y-%m-%d}", cwd=WORKTREE)
        local.sh("git", "push", "origin", "hd-inbox", cwd=WORKTREE)
        log("week-review запушен")
    else:
        log("без изменений")
    return 0


if __name__ == "__main__":
    sys.exit(main())
