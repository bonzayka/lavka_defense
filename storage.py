# -*- coding: utf-8 -*-
"""
Простое персистентное хранилище в data.json (рядом с ботом).
Переживает перезапуск: стоп-слова, варны, белый список ссылок, флаги-оверрайды.

Ключи (chat_id, user_id) хранятся строкой "chat:user", т.к. JSON не умеет кортежи.
"""

import json
import os
import threading

_LOCK = threading.Lock()
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

_DEFAULT = {
    "stopwords": [],          # список запрещённых слов/подстрок (нижний регистр)
    "warns": {},              # "chat:user" -> int
    "link_whitelist": [],     # ["chat:user", ...] — кому можно ссылки
    "flags": {},              # рантайм-оверрайды булевых настроек: name -> bool
}

_data: dict = {}


def _key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def load() -> None:
    global _data
    try:
        with open(_PATH, encoding="utf-8") as f:
            _data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _data = {}
    for k, v in _DEFAULT.items():
        _data.setdefault(k, json.loads(json.dumps(v)))


def save() -> None:
    with _LOCK:
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _PATH)


# --- стоп-слова ---

def stopwords() -> list[str]:
    return _data["stopwords"]


def add_stopword(word: str) -> bool:
    w = word.strip().lower()
    if not w or w in _data["stopwords"]:
        return False
    _data["stopwords"].append(w)
    save()
    return True


def del_stopword(word: str) -> bool:
    w = word.strip().lower()
    if w in _data["stopwords"]:
        _data["stopwords"].remove(w)
        save()
        return True
    return False


# --- варны ---

def get_warns(chat_id: int, user_id: int) -> int:
    return _data["warns"].get(_key(chat_id, user_id), 0)


def add_warn(chat_id: int, user_id: int) -> int:
    k = _key(chat_id, user_id)
    n = _data["warns"].get(k, 0) + 1
    _data["warns"][k] = n
    save()
    return n


def reset_warns(chat_id: int, user_id: int) -> None:
    _data["warns"].pop(_key(chat_id, user_id), None)
    save()


# --- белый список ссылок ---

def link_allowed(chat_id: int, user_id: int) -> bool:
    return _key(chat_id, user_id) in _data["link_whitelist"]


def allow_link(chat_id: int, user_id: int) -> bool:
    k = _key(chat_id, user_id)
    if k in _data["link_whitelist"]:
        return False
    _data["link_whitelist"].append(k)
    save()
    return True


def disallow_link(chat_id: int, user_id: int) -> bool:
    k = _key(chat_id, user_id)
    if k in _data["link_whitelist"]:
        _data["link_whitelist"].remove(k)
        save()
        return True
    return False


# --- флаги-оверрайды (вкл/выкл фич в рантайме поверх config) ---

def get_flag(name: str, default: bool) -> bool:
    return bool(_data["flags"].get(name, default))


def set_flag(name: str, value: bool) -> None:
    _data["flags"][name] = bool(value)
    save()
