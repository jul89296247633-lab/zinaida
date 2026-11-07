# -*- coding: utf-8 -*-
"""Проверка доставки: шлёт проверочное сообщение во все подключённые чаты
(TELEGRAM_CHAT_ID + TELEGRAM_ASSISTANT_CHAT_ID) бота трендс.

Запуск из воркфлоу tg-check.yml (разовый). Используется, чтобы убедиться,
что новый канал (например, помощница) получает сообщения Зинаиды.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
chats = [
    c
    for c in {
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        os.environ.get("TELEGRAM_ASSISTANT_CHAT_ID", ""),
    }
    if c
]
if not TOKEN or not chats:
    print("нет TELEGRAM_BOT_TOKEN или чатов — проверка пропущена", flush=True)
    sys.exit(0)

text = (
    "Тест доставки Зинаиды: канал подключён. "
    "Черновики ответов по задачам будут приходить сюда копией."
)
for chat in chats:
    query = urllib.parse.urlencode({"chat_id": chat, "text": text})
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage?{query}"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"чат {chat} -> HTTP {r.status}: {json.loads(r.read().decode()).get('ok')}", flush=True)
print("готово", flush=True)
