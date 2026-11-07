# -*- coding: utf-8 -*-
"""Отправить файл из репозитория в чаты бота трендс (Юлия + помощница).

Универсальный отправщик: env TG_FILE_PATH — путь к файлу в репо,
TG_CAPTION — подпись. Используется разовыми прогонами воркфлоу
(например, доставка библиотеки ответов).
"""
import os
import sys
import urllib.request

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHATS = [
    c
    for c in {
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        os.environ.get("TELEGRAM_ASSISTANT_CHAT_ID", ""),
    }
    if c
]
PATH = os.environ.get("TG_FILE_PATH", "")
CAPTION = os.environ.get("TG_CAPTION", "")


def main():
    if not (TG_TOKEN and TG_CHATS and PATH and os.path.exists(PATH)):
        print(f"нет файла {PATH!r} или секретов — отправка пропущена", flush=True)
        return 0 if not PATH else 1
    import requests

    for chat in TG_CHATS:
        with open(PATH, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": chat, "caption": CAPTION[:1000]},
                files={"document": (os.path.basename(PATH), f, "text/markdown")},
                timeout=120,
            )
            r.raise_for_status()
            print(f"чат …{chat[-4:]}: документ -> HTTP {r.status_code}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
