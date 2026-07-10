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
    "triggers": {},           # ключевое_слово -> ответ (автоответы)
    "mod_numbers": {},        # str(user_id) -> N (для анонимных «Модератор #N»)
    "roles": {},              # str(user_id) -> имя роли (внутренние права)
    "role_perms": {},         # имя_роли -> [список прав]; оверрайд config.ROLES из панели
    "role_titles": {},        # ключ_роли -> кастомное отображаемое имя ранга (переименование)
    "owners": [],             # [user_id, ...] — владельцы: только они раздают роли/должности
    "sticker_packs": [],      # [set_name, ...] — стикерпаки, которые НЕ проверять на 18+
    "verified_phones": {},    # str(user_id) -> {"prefix","tail","ts"} — прошли верификацию по номеру (глобально)
    "pending_verify": {},     # str(user_id) -> {"chat","notice","ts"} — ждут подтверждения номера в ЛС
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


# --- триггеры / автоответы ---

def triggers() -> dict:
    return _data.setdefault("triggers", {})


def add_trigger(key: str, reply: str) -> None:
    triggers()[key.strip().lower()] = reply
    save()


def del_trigger(key: str) -> bool:
    if key.strip().lower() in triggers():
        del triggers()[key.strip().lower()]
        save()
        return True
    return False


def mod_number(user_id: int) -> int:
    """Стабильный номер админа для анонимного режима («Модератор #N»)."""
    m = _data.setdefault("mod_numbers", {})
    k = str(user_id)
    if k not in m:
        m[k] = (max(m.values()) + 1) if m else 1
        save()
    return m[k]


# --- внутренние роли/права ---

def get_role(user_id: int) -> str | None:
    return _data.setdefault("roles", {}).get(str(user_id))


def set_role(user_id: int, role: str | None) -> None:
    r = _data.setdefault("roles", {})
    if role is None:
        r.pop(str(user_id), None)
    else:
        r[str(user_id)] = role
    save()


def roles_all() -> dict:
    return _data.setdefault("roles", {})


# --- оверрайд набора прав по ролям (редактируется из панели) ---

def get_role_perms_override(role: str) -> list | None:
    """Список прав роли, если он переопределён из панели, иначе None (=дефолт config)."""
    return _data.setdefault("role_perms", {}).get(role)


def set_role_perms(role: str, perms: list) -> None:
    _data.setdefault("role_perms", {})[role] = sorted(set(perms))
    save()


def role_perms_all() -> dict:
    return _data.setdefault("role_perms", {})


# --- кастомные названия рангов (только отображаемое имя; ключ роли не меняется) ---

def get_role_title(role: str) -> str | None:
    return _data.setdefault("role_titles", {}).get(role)


def set_role_title(role: str, title: str | None) -> None:
    t = _data.setdefault("role_titles", {})
    if title:
        t[role] = title
    else:
        t.pop(role, None)
    save()


def role_titles_all() -> dict:
    return _data.setdefault("role_titles", {})


# --- владельцы (только они выдают роли/должности) ---

def owners_all() -> list:
    return _data.setdefault("owners", [])


def is_owner(user_id: int) -> bool:
    return int(user_id) in _data.setdefault("owners", [])


def add_owner(user_id: int) -> bool:
    """True — добавили, False — уже был владельцем."""
    o = _data.setdefault("owners", [])
    if int(user_id) in o:
        return False
    o.append(int(user_id))
    save()
    return True


def remove_owner(user_id: int) -> bool:
    """True — сняли, False — не был владельцем."""
    o = _data.setdefault("owners", [])
    if int(user_id) in o:
        o.remove(int(user_id))
        save()
        return True
    return False


# --- белый список стикерпаков (не проверять на 18+) ---

def _norm_pack(set_name: str) -> str:
    return (set_name or "").strip().lower()


def sticker_packs() -> list:
    return _data.setdefault("sticker_packs", [])


def is_pack_allowed(set_name: str) -> bool:
    """True — стикерпак в белом списке (пропускать без NSFW-проверки)."""
    name = _norm_pack(set_name)
    return bool(name) and name in _data.setdefault("sticker_packs", [])


def allow_pack(set_name: str) -> bool:
    """True — добавили в белый список, False — уже был там (или пустое имя)."""
    name = _norm_pack(set_name)
    if not name:
        return False
    p = _data.setdefault("sticker_packs", [])
    if name in p:
        return False
    p.append(name)
    save()
    return True


def disallow_pack(set_name: str) -> bool:
    """True — убрали из белого списка, False — его там не было."""
    name = _norm_pack(set_name)
    p = _data.setdefault("sticker_packs", [])
    if name in p:
        p.remove(name)
        save()
        return True
    return False


# --- верификация по номеру телефона (глобально, по user_id) ---

def is_phone_verified(user_id: int) -> bool:
    return str(user_id) in _data.setdefault("verified_phones", {})


def set_phone_verified(user_id: int, prefix: str, tail: str, ts: str = "") -> None:
    """Пометить юзера прошедшим верификацию. Полный номер НЕ храним —
    только код страны (prefix) и 2 последние цифры (tail) для справки админу."""
    _data.setdefault("verified_phones", {})[str(user_id)] = {
        "prefix": prefix, "tail": tail, "ts": ts,
    }
    _data.setdefault("pending_verify", {}).pop(str(user_id), None)
    save()


def unverify_phone(user_id: int) -> bool:
    """Снять верификацию (напр. если админ хочет заставить перепройти)."""
    if str(user_id) in _data.setdefault("verified_phones", {}):
        del _data["verified_phones"][str(user_id)]
        save()
        return True
    return False


def verified_count() -> int:
    return len(_data.setdefault("verified_phones", {}))


# --- ожидающие подтверждения номера (переживает перезапуск) ---

def set_pending_verify(user_id: int, chat_id: int, notice_id=None, ts: str = "") -> None:
    _data.setdefault("pending_verify", {})[str(user_id)] = {
        "chat": chat_id, "notice": notice_id, "ts": ts,
    }
    save()


def get_pending_verify(user_id: int) -> dict | None:
    return _data.setdefault("pending_verify", {}).get(str(user_id))


def clear_pending_verify(user_id: int) -> None:
    if str(user_id) in _data.setdefault("pending_verify", {}):
        del _data["pending_verify"][str(user_id)]
        save()


# --- журнал действий (audit log) ---
def add_audit(entry: dict) -> None:
    log = _data.setdefault("audit", [])
    log.append(entry)
    del log[:-AUDIT_LIMIT]  # держим только последние AUDIT_LIMIT
    save()


def get_audit(n: int = 15) -> list[dict]:
    return list(reversed(_data.get("audit", [])[-n:]))
