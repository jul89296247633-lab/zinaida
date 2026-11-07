# -*- coding: utf-8 -*-
"""Локальный сборщик задач hd.borboza → ветка hd-inbox репозитория Зинаиды.

Почему локально: с 06.11 порталы Борбозы не отвечают раннерам GitHub.
Правило Юлии: сбор Борбозы — через Яндекс-Браузер (HD_BROWSER_PATH +
HD_PROFILE_DIR, изолированный профиль, без турбо/VPN).

Что делает каждый час (планировщик Windows, рабочие часы МСК):
  1) Яндекс-Браузер: автологин по регламенту + первые страницы «Входящих»;
  2) свежие задачи (сортировка по последнему сообщению) → hd-inbox/latest.json
     в отдельном worktree ветки hd-inbox;
  3) git push origin hd-inbox.

GitHub-агент (hd-answers.yml) читает этот файл вместо портала; черновики
GLM и отправка в Telegram остаются на GitHub (ключи только там).
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hd_tasks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKTREE = ROOT / ".hd-inbox-worktree"
INBOX_DIR = "hd-inbox"
PAGES = int(os.environ.get("HD_TASKS_PAGES", "3"))  # настройка hd_tasks.PAGES; сборщик читает его сам
STALE_H = 3  # GitHub-агент игнорирует Inbox старше N часов


def log(msg):
    print(f"[hd-collect-local] {msg}", flush=True)


def sh(*args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def prepare_worktree():
    if not (WORKTREE / ".git").exists():
        sh("git", "fetch", "origin", "main", cwd=ROOT)
        branches = sh("git", "branch", "--list", "hd-inbox", cwd=ROOT)
        if branches:
            sh("git", "worktree", "add", str(WORKTREE), "hd-inbox", cwd=ROOT)
        else:
            sh("git", "worktree", "add", "-b", "hd-inbox", str(WORKTREE), "origin/main", cwd=ROOT)
    try:
        sh("git", "fetch", "origin", "hd-inbox", cwd=WORKTREE)
    except RuntimeError as e:
        if "couldn't find remote ref" not in str(e):
            raise  # ветки ещё нет — это нормальный первый запуск
    if _remote_exists():
        local = sh("git", "rev-parse", "HEAD", cwd=WORKTREE)
        remote = sh("git", "rev-parse", "origin/hd-inbox", cwd=WORKTREE)
        if local != remote:
            sh("git", "reset", "--hard", "origin/hd-inbox", cwd=WORKTREE)


def _remote_exists():
    try:
        sh("git", "rev-parse", "--verify", "origin/hd-inbox", cwd=WORKTREE)
        return True
    except RuntimeError:
        return False


def load_env_fallback():
    """Креды Борбозы из collector/.env, если их нет в окружении (планировщик)."""
    if os.environ.get("BORBOZA_LOGIN"):
        return
    env_path = Path("C:/Users/user/Desktop/id-tools/collector/.env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf8", errors="replace").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env_fallback()
    now = dt.datetime.now(hd_tasks.MSK)
    if now.hour < 7 or now.hour >= 22:
        log(f"{now:%H:%M} вне рабочих часов — пропуск")
        return 0

    username = os.environ.get("BORBOZA_LOGIN")
    password = os.environ.get("BORBOZA_PASSWORD")
    if not (username and password):
        print("нет BORBOZA_LOGIN/BORBOZA_PASSWORD", flush=True)
        return 1

    tasks = hd_tasks.collect(username, password, now - dt.timedelta(hours=72))
    log(f"задач собрано: {len(tasks)}")

    prepare_worktree()
    out = WORKTREE / INBOX_DIR
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "collected_at": now.isoformat(),
        "stale_after_h": STALE_H,
        "tasks": tasks,
    }
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf8")

    sh("git", "add", INBOX_DIR, cwd=WORKTREE)
    if not sh("git", "status", "--porcelain", cwd=WORKTREE):
        log("без изменений — не коммитим")
        return 0
    sh("git", "-c", "user.name=Zinaida Local Collector",
       "-c", "user.email=zinaida-local@users.noreply.github.com",
       "commit", "-m", f"inbox {now:%Y-%m-%d %H:%M}", cwd=WORKTREE)
    sh("git", "push", "origin", "hd-inbox", cwd=WORKTREE)
    log("запушено в hd-inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
