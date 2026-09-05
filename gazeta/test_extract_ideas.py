# -*- coding: utf-8 -*-
"""Синтетический тест extract_ideas (без сети и без GLM)."""
import sys
sys.path.insert(0, ".")

MD = """## 01 · Тренды AI

### Anthropic выпустила Claude 5 с окном 1M токенов
Что произошло: окно выросло вчетверо, цены не изменились.
Это меняет экономику длинных документов.
*Источник: Habr — Искусственный интеллект (https://habr.com/p/123456)*
> **ИДЕЯ КОНТЕНТА.** Карусель «1M токенов: что это значит для вашего бюджета на ИИ» — разбор на цифрах.

### Новость без идеи
Просто текст.
*Источник: vc.ru*

## 02 · Модели

### GLM-4.7 подешевел на 30%
Цена упала, скорость та же.
*Источник: vc.ru*
> **ИДЕЯ КОНТЕНТА.** Пост «пересчитайте свой пайплайн на GLM» с таблицей «было/стало».

## 05 · Находки с GitHub

### super-tool — генератор агентов
Чем примечателен.
> **Зачем ей:** для консалтга.
*Источник: github.com/foo/bar*

## 06 · Приём дня
Шаги.
"""

MATERIAL = {
    "Habr — Искусственный интеллект": [
        {"title": "Claude 5", "url": "https://habr.com/p/123456", "date": "", "summary": ""},
    ],
    "vc.ru": [
        {"title": "GLM дешевле", "url": "https://vc.ru/p/777", "date": "", "summary": ""},
        {"title": "другое", "url": "https://vc.ru/p/888", "date": "", "summary": ""},
    ],
}

from gazeta.main import extract_ideas

ideas = extract_ideas(MD, MATERIAL)
for it in ideas:
    print("-", it["title"], "|", it["url"], "|", it["summary"][:80])

assert len(ideas) == 2, f"ожидалось 2 идеи, получено {len(ideas)}"
assert ideas[0]["title"].startswith("Anthropic"), ideas[0]
assert ideas[0]["url"] == "https://habr.com/p/123456", ideas[0]["url"]  # URL из строки источника
assert "1M токенов" in ideas[0]["summary"], ideas[0]
assert ideas[1]["url"] == "https://vc.ru/p/777", ideas[1]["url"]  # FIFO по ленте vc.ru
assert "пересчитайте" in ideas[1]["summary"]
assert all("super-tool" not in i["title"] for i in ideas)  # секция 05 не берётся
print("EXTRACT_IDEAS_TEST_OK")
