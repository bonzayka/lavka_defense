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
# Путь к данным можно задать через env DATA_FILE (для дочерних ботов — свой файл).
_PATH = os.environ.get("DATA_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data.json")

_DEFAULT = {
    "stopwords": [],          # список запрещённых слов/подстрок (нижний регистр)
    "warns": {},              # "chat:user" -> int
    "link_whitelist": [],     # ["chat:user", ...] — кому можно ссылки
    "trusted": [],            # ["chat:user", ...] — «свои», мимо всех проверок
    "flags": {},              # рантайм-оверрайды булевых настроек: name -> bool
    "nums": {},               # рантайм-оверрайды числовых настроек: name -> int
    "strs": {},               # рантайм-оверрайды строковых (действия и т.п.)
    "stats": {},              # сохранённая статистика
    "rules": "",              # текст правил группы
    "audit": [],              # журнал действий модерации (последние N)
}

AUDIT_LIMIT = 200

def _fresh() -> dict:
    return {k: json.loads(json.dumps(v)) for k, v in _DEFAULT.items()}


_data: dict = _fresh()  # безопасно ещё до load()


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


# --- доверенные пользователи (мимо всех проверок) ---

def is_trusted(chat_id: int, user_id: int) -> bool:
    return _key(chat_id, user_id) in _data["trusted"]


def toggle_trusted(chat_id: int, user_id: int) -> bool:
    """Вернёт True если добавили, False если убрали."""
    k = _key(chat_id, user_id)
    if k in _data["trusted"]:
        _data["trusted"].remove(k)
        save()
        return False
    _data["trusted"].append(k)
    save()
    return True


# --- флаги/числа/строки-оверрайды (поверх config, меняются в рантайме) ---

def get_flag(name: str, default: bool) -> bool:
    return bool(_data["flags"].get(name, default))


def set_flag(name: str, value: bool) -> None:
    _data["flags"][name] = bool(value)
    save()


def get_num(name: str, default: int) -> int:
    return int(_data["nums"].get(name, default))


def set_num(name: str, value: int) -> None:
    _data["nums"][name] = int(value)
    save()


def get_str(name: str, default: str) -> str:
    return str(_data["strs"].get(name, default))


def set_str(name: str, value: str) -> None:
    _data["strs"][name] = str(value)
    save()


# --- правила группы ---

def get_rules() -> str:
    return _data.get("rules", "")


def set_rules(text: str) -> None:
    _data["rules"] = text
    save()


# --- статистика (переживает перезапуск) ---

def load_stats() -> dict:
    return dict(_data.get("stats", {}))


def save_stats(stats: dict) -> None:
    _data["stats"] = dict(stats)
    save()


# --- журнал действий (audit log) ---

def add_audit(entry: dict) -> None:
    log = _data.setdefault("audit", [])
    log.append(entry)
    del log[:-AUDIT_LIMIT]  # держим только последние AUDIT_LIMIT
    save()


def get_audit(n: int = 15) -> list[dict]:
    return list(reversed(_data.get("audit", [])[-n:]))
