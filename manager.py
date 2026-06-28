# -*- coding: utf-8 -*-
"""
Менеджер дочерних ботов («древо»). Каждый добавленный токен запускается как
ОТДЕЛЬНЫЙ процесс того же bot.py, но со своим data.json и папкой картинок —
полная изоляция, боты не пересекаются.

Список ботов хранится в children.json (только на стороне родителя).
Дочерние процессы помечены env CHILD_BOT=1 и менеджер у себя не поднимают.
"""

import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CHILDREN_FILE = os.path.join(BASE, "children.json")
CHILDREN_DIR = os.path.join(BASE, "children")
BOT_SCRIPT = os.path.join(BASE, "bot.py")

# По умолчанию у дочерних ботов NudeNet выключен (экономия ОЗУ). Поставь True,
# если ОЗУ хватает и нужна проверка 18+ на каждом боте.
CHILD_NSFW = False

TOKEN_RE = re.compile(r"^\d{6,}:[\w-]{30,}$")

_procs: dict[str, subprocess.Popen] = {}


def is_child() -> bool:
    return bool(os.environ.get("CHILD_BOT"))


def valid_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token.strip()))


def bot_id(token: str) -> str:
    return token.split(":")[0]


def _load() -> list[dict]:
    try:
        with open(CHILDREN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    with open(CHILDREN_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _paths(bid: str) -> tuple[str, str]:
    d = os.path.join(CHILDREN_DIR, bid)
    photo = os.path.join(d, "photo")
    os.makedirs(photo, exist_ok=True)
    return os.path.join(d, "data.json"), photo


def alive(bid: str) -> bool:
    p = _procs.get(bid)
    return p is not None and p.poll() is None


def spawn(entry: dict) -> None:
    bid = entry["id"]
    if alive(bid):
        return
    data_file, photo = _paths(bid)
    env = dict(os.environ)
    env.update({
        "BOT_TOKEN": entry["token"],
        "DATA_FILE": data_file,
        "PHOTO_DIR": photo,
        "CHILD_BOT": "1",
        "NSFW_ENABLED": "1" if CHILD_NSFW else "0",
    })
    _procs[bid] = subprocess.Popen([sys.executable, BOT_SCRIPT], env=env)


def start_all() -> int:
    items = _load()
    for e in items:
        try:
            spawn(e)
        except Exception:
            pass
    return len(items)


def stop(bid: str) -> None:
    p = _procs.pop(bid, None)
    if p and p.poll() is None:
        p.terminate()


def stop_all() -> None:
    for bid in list(_procs):
        stop(bid)


def add(token: str, username: str = "") -> bool:
    """Добавить и запустить бота. False — если уже есть."""
    token = token.strip()
    bid = bot_id(token)
    items = _load()
    if any(i["id"] == bid for i in items):
        return False
    entry = {"id": bid, "token": token, "username": username}
    items.append(entry)
    _save(items)
    spawn(entry)
    return True


def remove(bid: str) -> None:
    stop(bid)
    _save([i for i in _load() if i["id"] != bid])


def children() -> list[dict]:
    """Список с пометкой alive для панели."""
    return [{**e, "alive": alive(e["id"])} for e in _load()]
