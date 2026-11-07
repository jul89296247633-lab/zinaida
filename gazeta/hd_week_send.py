# -*- coding: utf-8 -*-
"""Отправка недельного обзора задач в Telegram (оба чата: Юлия + помощница).

Читает hd-inbox/week-review.json (готовит локальный прогон hd_week_review.py
на машине Юлии через Яндекс-Браузер); свежесть <=36 ч. Протух или нет —
сообщаем в чат короткую причину вместо молчания.
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
WEEK_API = os.environ.get("HD_WEEK_API", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHATS = [
    c
    for c in {
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        os.environ.get("TELEGRAM_ASSISTANT_CHAT_ID", ""),
    }
    if c
]


def log(msg):
    print(f"[hd-week-send] {msg}", flush=True)


def main():
    if not (WEEK_API and TG_TOKEN and TG_CHATS):
        print("нет HD_WEEK_API или Telegram-секретов — отправка пропущена", flush=True)
        return 0

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(WEEK_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            meta = json.loads(r.read().decode())
        data = json.loads(base64.b64decode(meta["content"]).decode("utf8"))
    except Exception as e:
        log(f"обзор недоступен: {e}")
        return 0

    generated = dt.datetime.fromisoformat(data.get("generated_at", ""))
    if (dt.datetime.now(MSK) - generated).total_seconds() > 36 * 3600:
        text = "Зинаида: недельный обзор задач не собрался (локальный сборщик не обновлял ветку >36 ч)."
        log("обзор протух")
    else:
        text = f"📋 Обзор задач за неделю ({data.get('week_from', '')} …)\n\n" + data.get("text", "")

    for chat in TG_CHATS:
        body = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"чат …{chat[-4:]} -> HTTP {r.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
