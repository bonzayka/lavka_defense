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

# По умолчанию у дочерних ботов тяжёлые ИИ-модели выключены (экономия ОЗУ):
# NudeNet (~400 МБ) и CLIP-гор (~1 ГБ). Включи, если памяти хватает.
CHILD_NSFW = False
CHILD_GORE = False

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
        "GORE_ENABLED": "1" if CHILD_GORE else "0",
        # У каждого дочернего — СВОЙ пароль панели (не родительский «Benny»).
        "PANEL_PASSWORD": entry.get("password") or "",
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


def add(token: str, username: str = "", owner: int = 0, password: str = "") -> bool:
    """Добавить и запустить бота. False — если уже есть. owner — id создателя."""
    token = token.strip()
    bid = bot_id(token)
    items = _load()
    if any(i["id"] == bid for i in items):
        return False
    entry = {"id": bid, "token": token, "username": username,
             "owner": owner, "password": password}
    items.append(entry)
    _save(items)
    spawn(entry)
    return True


def owns(bid: str, owner: int) -> bool:
    """Принадлежит ли бот этому владельцу."""
    return any(i["id"] == bid and i.get("owner") == owner for i in _load())


def remove(bid: str, owner: int | None = None) -> bool:
    """Удалить бота. Если owner задан — только если он владелец."""
    if owner is not None and not owns(bid, owner):
        return False
    stop(bid)
    _save([i for i in _load() if i["id"] != bid])
    return True


def children(owner: int | None = None) -> list[dict]:
    """Список ботов (с пометкой alive). Если owner задан — только его боты."""
    items = _load()
    if owner is not None:
        items = [i for i in items if i.get("owner") == owner]
    return [{**e, "alive": alive(e["id"])} for e in items]
