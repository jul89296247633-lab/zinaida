# -*- coding: utf-8 -*-
"""Показать, кто писал боту: chat_id + имя + username из getUpdates.

Нужен, чтобы узнать chat_id помощницы: она пишет боту любое сообщение
(например /start), затем этот скрипт (воркфлоу tg-chat-lookup) печатает
её chat_id — его кладём в секрет TELEGRAM_ASSISTANT_CHAT_ID.

getUpdates хранит обновления 24 часа и не работает, если у бота стоит
webhook — тогда печатаем описание вебхука как подсказку.
"""
import json
import os
import sys
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    print("нет TELEGRAM_BOT_TOKEN", flush=True)
    return_code = 1
    sys.exit(return_code)


def call(method):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", timeout=30
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


me = call("getMe")
print(f"бот: @{me.get('username')} ({me.get('first_name', '')})", flush=True)

try:
    updates = call("getUpdates?allowed_updates=%5B%22message%22%2C%22channel_post%22%5D")
except Exception as e:  # noqa: BLE001
    print(f"getUpdates не удался: {e}", flush=True)
    sys.exit(0)

seen = {}
for u in updates.get("result", []):
    msg = u.get("message") or u.get("channel_post") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is None:
        continue
    seen[cid] = {
        "type": chat.get("type"),
        "title": chat.get("title") or chat.get("first_name", ""),
        "username": chat.get("username", ""),
        "text": (msg.get("text") or "")[:40],
        "date": msg.get("date"),
    }

if not seen:
    print("писавших боту за последние 24 ч не видно", flush=True)
else:
    print("чат_id | тип | имя | @username | сообщение", flush=True)
    for cid, info in seen.items():
        print(f"{cid} | {info['type']} | {info['title']} | @{info['username']} | {info['text']}", flush=True)
