# -*- coding: utf-8 -*-
"""
Хранилище данных Lavka Defense:
- SQLite (data.db) в режиме WAL с индексами для надёжности и ACID-сохранности.
- In-memory кэш (_data) для сверхбыстрых проверок за 0 мс без дисковых задержек.
- Автоматическая синхронизация с data.json для совместимости и бэкапов.
"""

import copy
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime

import config

log = logging.getLogger("antispam.storage")


_LOCK = threading.Lock()

def _get_path() -> str:
    return os.environ.get("DATA_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data.json")

def _get_db_path() -> str:
    p = _get_path()
    return p.rsplit(".", 1)[0] + ".db"

_PATH = _get_path()
_DB_PATH = _get_db_path()

_DEFAULT = {
    "stopwords": [],          # список запрещённых слов/подстрок (нижний регистр)
    "hidden_words": [],       # подмножество stopwords, которые НЕ показываем публично (анонимный бан)
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
    "activity": {},           # "chat:user" -> {"msgs","first_seen","last_seen"} — активность (старожилы + бэкап)
    "reputation": {},         # "chat:user" -> {"score","plus","minus"} — репутация (+реп/-реп)
    "rep_quota": {},          # "chat:giver" -> {"day":"YYYY-MM-DD","count":N,"targets":[...]} — суточная квота выдач (сброс в 00:00 МСК)
    "awards": {},             # str(user_id) -> [{"text","by","ts"}] — награды (/наградить), текст свободный
    "flood_penalties": {},    # "chat:user" -> iso_timestamp_until — штраф за флуд (автоудаление сообщений)
    "usernames": {},          # username.lower() -> user_id (для таргета по @нику)
    "user_to_uname": {},      # str(user_id) -> username.lower()
    "user_perms": {},         # str(user_id) -> [perms] (персональные права)
}

AUDIT_LIMIT = 200

def _fresh() -> dict:
    return copy.deepcopy(_DEFAULT)

_data: dict = _fresh()  # безопасно ещё до load()

# Быстрые in-memory множества для O(1) проверок
_link_whitelist_set: set[str] = set()
_trusted_set: set[str] = set()
_owners_set: set[int] = set()
_stopwords_set: set[str] = set()
_hidden_words_set: set[str] = set()
_sticker_packs_set: set[str] = set()
_stopwords_tuples: list[tuple[str, str, str]] = []

def _squeeze_simple(s: str) -> str:
    out = []
    prev = ""
    for ch in s:
        if ch != prev:
            out.append(ch)
            prev = ch
    return "".join(out)

def _rebuild_indices() -> None:
    global _link_whitelist_set, _trusted_set, _owners_set
    global _stopwords_set, _hidden_words_set, _sticker_packs_set, _stopwords_tuples
    _link_whitelist_set = set(_data.get("link_whitelist", []))
    _trusted_set = set(_data.get("trusted", []))
    _owners_set = {int(u) for u in _data.get("owners", [])}
    _stopwords_set = {w.lower() for w in _data.get("stopwords", [])}
    _hidden_words_set = {w.lower() for w in _data.get("hidden_words", [])}
    _sticker_packs_set = {(p or "").strip().lower() for p in _data.get("sticker_packs", []) if p}
    _stopwords_tuples = [
        (w, w.lower(), _squeeze_simple(w.lower()))
        for w in _data.get("stopwords", [])
        if w and w.strip()
    ]

_rebuild_indices()

def _key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"

_conn: sqlite3.Connection | None = None
_conn_db_path: str | None = None

def _init_sqlite_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS activity (
        chat_id INTEGER,
        user_id INTEGER,
        first_seen TEXT,
        msgs INTEGER DEFAULT 0,
        last_seen TEXT,
        PRIMARY KEY (chat_id, user_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usernames (
        username TEXT PRIMARY KEY,
        user_id INTEGER
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_to_uname (
        user_id INTEGER PRIMARY KEY,
        username TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS warns (
        chat_id INTEGER,
        user_id INTEGER,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reputation (
        chat_id INTEGER,
        user_id INTEGER,
        score INTEGER DEFAULT 0,
        plus INTEGER DEFAULT 0,
        minus INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rep_quota (
        chat_id INTEGER,
        giver_id INTEGER,
        day TEXT,
        count INTEGER DEFAULT 0,
        targets_json TEXT DEFAULT '[]',
        PRIMARY KEY (chat_id, giver_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS awards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT,
        by_id INTEGER,
        by_name TEXT,
        ts TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        actor TEXT,
        action TEXT,
        target_id INTEGER,
        target_name TEXT,
        reason TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS verified_phones (
        user_id INTEGER PRIMARY KEY,
        prefix TEXT,
        tail TEXT,
        ts TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_verify (
        user_id INTEGER PRIMARY KEY,
        chat INTEGER,
        notice INTEGER,
        prefix TEXT DEFAULT '',
        tail TEXT DEFAULT '',
        review INTEGER DEFAULT 0,
        ts TEXT
    );
    """)
    existing_cols = {col[1] for col in cur.execute("PRAGMA table_info(pending_verify)").fetchall()}
    for col_name, col_def in [("prefix", "TEXT DEFAULT ''"), ("tail", "TEXT DEFAULT ''"), ("review", "INTEGER DEFAULT 0")]:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE pending_verify ADD COLUMN {col_name} {col_def}")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS flood_penalties (
        chat_id INTEGER,
        user_id INTEGER,
        until_iso TEXT,
        PRIMARY KEY (chat_id, user_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value_json TEXT
    );
    """)
    conn.commit()

def _get_connection() -> sqlite3.Connection:
    global _conn, _conn_db_path, _PATH, _DB_PATH
    _PATH = _get_path()
    current_db = _get_db_path()
    _DB_PATH = current_db
    if _conn is None or _conn_db_path != current_db:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = sqlite3.connect(current_db, check_same_thread=False)
        _conn_db_path = current_db
        _init_sqlite_schema(_conn)
    return _conn

def _import_json_to_db(conn: sqlite3.Connection, data: dict) -> None:
    cur = conn.cursor()
    # 1. Activity
    for k, v in data.get("activity", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            try:
                cid, uid = int(parts[0]), int(parts[1])
                cur.execute("""
                INSERT OR REPLACE INTO activity (chat_id, user_id, first_seen, msgs, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """, (cid, uid, v.get("first_seen", ""), v.get("msgs", 0), v.get("last_seen", "")))
            except Exception:
                pass

    # 2. Usernames
    for uname, uid in data.get("usernames", {}).items():
        try:
            clean_uname = uname.lstrip("@").strip().lower()
            cur.execute("INSERT OR REPLACE INTO usernames (username, user_id) VALUES (?, ?)", (clean_uname, int(uid)))
        except Exception:
            pass
    for uid, uname in data.get("user_to_uname", {}).items():
        try:
            clean_uname = uname.lstrip("@").strip().lower()
            cur.execute("INSERT OR REPLACE INTO user_to_uname (user_id, username) VALUES (?, ?)", (int(uid), clean_uname))
        except Exception:
            pass

    # 3. Warns
    for k, count in data.get("warns", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            try:
                cid, uid = int(parts[0]), int(parts[1])
                cur.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, count) VALUES (?, ?, ?)", (cid, uid, count))
            except Exception:
                pass

    # 4. Reputation
    for k, v in data.get("reputation", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            try:
                cid, uid = int(parts[0]), int(parts[1])
                cur.execute("""
                INSERT OR REPLACE INTO reputation (chat_id, user_id, score, plus, minus)
                VALUES (?, ?, ?, ?, ?)
                """, (cid, uid, v.get("score", 0), v.get("plus", 0), v.get("minus", 0)))
            except Exception:
                pass

    # 5. Rep quota
    for k, v in data.get("rep_quota", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            try:
                cid, gid = int(parts[0]), int(parts[1])
                cur.execute("""
                INSERT OR REPLACE INTO rep_quota (chat_id, giver_id, day, count, targets_json)
                VALUES (?, ?, ?, ?, ?)
                """, (cid, gid, v.get("day", ""), v.get("count", 0), json.dumps(v.get("targets", []))))
            except Exception:
                pass

    # 6. Awards
    for uid_str, awards_list in data.get("awards", {}).items():
        try:
            uid = int(uid_str)
            for a in awards_list:
                cur.execute("""
                INSERT INTO awards (user_id, text, by_id, by_name, ts)
                VALUES (?, ?, ?, ?, ?)
                """, (uid, a.get("text", ""), a.get("by", 0), a.get("by_name", ""), a.get("ts", "")))
        except Exception:
            pass

    # 7. Audit
    for entry in data.get("audit", []):
        try:
            cur.execute("""
            INSERT INTO audit (ts, actor, action, target_id, target_name, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (entry.get("ts", ""), entry.get("actor", ""), entry.get("action", ""),
                  entry.get("target_id", 0), entry.get("target_name", ""), entry.get("reason", "")))
        except Exception:
            pass

    # 8. Verified phones & pending verify
    for uid_str, rec in data.get("verified_phones", {}).items():
        try:
            cur.execute("""
            INSERT OR REPLACE INTO verified_phones (user_id, prefix, tail, ts)
            VALUES (?, ?, ?, ?)
            """, (int(uid_str), rec.get("prefix", ""), rec.get("tail", ""), rec.get("ts", "")))
        except Exception:
            pass
    for uid_str, rec in data.get("pending_verify", {}).items():
        try:
            cur.execute("""
            INSERT OR REPLACE INTO pending_verify (user_id, chat, notice, prefix, tail, review, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (int(uid_str), rec.get("chat", 0), rec.get("notice", 0), rec.get("prefix", ""),
                  rec.get("tail", ""), 1 if rec.get("review") else 0, rec.get("ts", "")))
        except Exception:
            pass

    # 9. Flood penalties
    for k, until in data.get("flood_penalties", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            try:
                cid, uid = int(parts[0]), int(parts[1])
                cur.execute("INSERT OR REPLACE INTO flood_penalties (chat_id, user_id, until_iso) VALUES (?, ?, ?)",
                            (cid, uid, str(until)))
            except Exception:
                pass

    # 10. KV Store
    kv_keys = [
        "stopwords", "hidden_words", "link_whitelist", "trusted", "flags",
        "nums", "strs", "stats", "rules", "triggers", "mod_numbers", "roles",
        "role_perms", "role_titles", "owners", "sticker_packs", "user_perms"
    ]
    for k in kv_keys:
        val = data.get(k, [] if k in ("stopwords", "hidden_words", "link_whitelist", "trusted", "owners", "sticker_packs") else ("" if k == "rules" else {}))
        cur.execute("INSERT OR REPLACE INTO kv_store (key, value_json) VALUES (?, ?)", (k, json.dumps(val, ensure_ascii=False)))

    conn.commit()

def load() -> None:
    global _data, _PATH, _DB_PATH, _conn, _conn_db_path
    _PATH = _get_path()
    _DB_PATH = _get_db_path()

    with _LOCK:
        # If running under a custom DATA_FILE (e.g. tests.py) and DATA_FILE was removed,
        # reset any leftover test database so tests start with a clean state.
        if os.environ.get("DATA_FILE") and not os.path.exists(_PATH):
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
                _conn_db_path = None
            for f in (_DB_PATH, _DB_PATH + "-wal", _DB_PATH + "-shm"):
                try:
                    os.remove(f)
                except OSError:
                    pass

        conn = _get_connection()
        cur = conn.cursor()

        has_kv = cur.execute("SELECT 1 FROM kv_store LIMIT 1").fetchone()
        has_act = cur.execute("SELECT 1 FROM activity LIMIT 1").fetchone()
        migrated = False
        if not has_kv and not has_act and os.path.exists(_PATH):
            try:
                with open(_PATH, encoding="utf-8") as f:
                    json_data = json.load(f)
                _import_json_to_db(conn, json_data)
                migrated = True
            except Exception as e:
                log.warning("Не удалось импортировать данные из %s: %s", _PATH, e)


        d = _fresh()

        # 1. kv_store
        for key, value_json in cur.execute("SELECT key, value_json FROM kv_store").fetchall():
            try:
                d[key] = json.loads(value_json)
            except Exception:
                pass

        # 2. activity
        for cid, uid, fs, msgs, ls in cur.execute("SELECT chat_id, user_id, first_seen, msgs, last_seen FROM activity").fetchall():
            d["activity"][f"{cid}:{uid}"] = {
                "first_seen": fs or "",
                "msgs": int(msgs or 0),
                "last_seen": ls or ""
            }

        # 3. usernames & user_to_uname
        for uname, uid in cur.execute("SELECT username, user_id FROM usernames").fetchall():
            d["usernames"][uname] = int(uid)
        for uid, uname in cur.execute("SELECT user_id, username FROM user_to_uname").fetchall():
            d["user_to_uname"][str(uid)] = uname

        # 4. warns
        for cid, uid, count in cur.execute("SELECT chat_id, user_id, count FROM warns").fetchall():
            d["warns"][f"{cid}:{uid}"] = int(count)

        # 5. reputation
        for cid, uid, score, plus, minus in cur.execute("SELECT chat_id, user_id, score, plus, minus FROM reputation").fetchall():
            d["reputation"][f"{cid}:{uid}"] = {
                "score": int(score or 0),
                "plus": int(plus or 0),
                "minus": int(minus or 0)
            }

        # 6. rep_quota
        for cid, gid, day, count, targets_json in cur.execute("SELECT chat_id, giver_id, day, count, targets_json FROM rep_quota").fetchall():
            try:
                targets = json.loads(targets_json)
            except Exception:
                targets = []
            d["rep_quota"][f"{cid}:{gid}"] = {
                "day": day or "",
                "count": int(count or 0),
                "targets": targets
            }

        # 7. awards
        for uid, text, by_id, by_name, ts in cur.execute("SELECT user_id, text, by_id, by_name, ts FROM awards ORDER BY id ASC").fetchall():
            d["awards"].setdefault(str(uid), []).append({
                "text": text or "",
                "by": int(by_id or 0),
                "by_name": by_name or "",
                "ts": ts or ""
            })

        # 8. audit
        for ts, actor, action, target_id, target_name, reason in cur.execute(
            "SELECT ts, actor, action, target_id, target_name, reason FROM audit ORDER BY id ASC"
        ).fetchall():
            d["audit"].append({
                "ts": ts or "",
                "actor": actor or "",
                "action": action or "",
                "target_id": target_id if target_id is not None else 0,
                "target_name": target_name or "",
                "reason": reason or ""
            })
        if len(d["audit"]) > AUDIT_LIMIT:
            del d["audit"][:-AUDIT_LIMIT]

        # 9. verified_phones & pending_verify
        for uid, prefix, tail, ts in cur.execute("SELECT user_id, prefix, tail, ts FROM verified_phones").fetchall():
            d["verified_phones"][str(uid)] = {"prefix": prefix or "", "tail": tail or "", "ts": ts or ""}

        for uid, chat, notice, prefix, tail, review, ts in cur.execute(
            "SELECT user_id, chat, notice, prefix, tail, review, ts FROM pending_verify"
        ).fetchall():
            rec = {"chat": int(chat or 0), "notice": notice, "ts": ts or ""}
            if review:
                rec["review"] = True
                rec["prefix"] = prefix or ""
                rec["tail"] = tail or ""
            d["pending_verify"][str(uid)] = rec

        # 10. flood_penalties
        for cid, uid, until_iso in cur.execute("SELECT chat_id, user_id, until_iso FROM flood_penalties").fetchall():
            d["flood_penalties"][f"{cid}:{uid}"] = until_iso or ""

        _data = d
        _rebuild_indices()
        if migrated:
            log.info("Первый запуск: БД успешно перенесена из %s в SQLite (%s)", _PATH, _DB_PATH)
        log.info("Хранилище SQLite (%s) загружено: %d активных записей, %d варнов, %d юзеров, %d стоп-слов.",
                 os.path.basename(_DB_PATH), len(d["activity"]), len(d["warns"]), len(d["usernames"]), len(d["stopwords"]))

def save() -> None:
    with _LOCK:
        try:
            conn = _get_connection()
            conn.commit()
        except Exception:
            pass
        try:
            tmp = _PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _PATH)
        except Exception:
            pass

def _save_kv(key: str) -> None:
    val = _data.get(key)
    conn = _get_connection()
    with _LOCK:
        conn.execute("INSERT OR REPLACE INTO kv_store (key, value_json) VALUES (?, ?)",
                     (key, json.dumps(val, ensure_ascii=False)))
        conn.commit()

# --- стоп-слова ---

def stopwords() -> list[str]:
    return _data["stopwords"]

def stopwords_processed() -> list[tuple[str, str, str]]:
    """Предвычисленные кортежи (оригинал, lower, squeezed) для быстрого поиска."""
    return _stopwords_tuples

def del_stopword(word: str) -> bool:
    global _stopwords_tuples
    w = word.strip().lower()
    if w in _stopwords_set or w in _data["stopwords"]:
        if w in _data["stopwords"]:
            _data["stopwords"].remove(w)
        _stopwords_set.discard(w)
        _stopwords_tuples = [t for t in _stopwords_tuples if t[1] != w]
        _data.setdefault("hidden_words", [])
        if w in _data["hidden_words"]:
            _data["hidden_words"].remove(w)
        _hidden_words_set.discard(w)
        _save_kv("stopwords")
        _save_kv("hidden_words")
        save()
        return True
    return False

# --- скрытые (анонимные) стоп-слова: срабатывают, но не показываются в чате ---

def hidden_words() -> list[str]:
    return _data.setdefault("hidden_words", [])

def is_hidden_word(word: str) -> bool:
    return (word or "").strip().lower() in _hidden_words_set

def set_hidden_word(word: str, hidden: bool) -> bool:
    """Пометить стоп-слово скрытым/видимым. True — если состояние изменилось."""
    w = (word or "").strip().lower()
    if not w or w not in _stopwords_set:
        return False
    h = _data.setdefault("hidden_words", [])
    if hidden and w not in _hidden_words_set:
        h.append(w)
        _hidden_words_set.add(w)
        _save_kv("hidden_words")
        save()
        return True
    if not hidden and w in _hidden_words_set:
        if w in h:
            h.remove(w)
        _hidden_words_set.discard(w)
        _save_kv("hidden_words")
        save()
        return True
    return False

def add_stopword(word: str, hidden: bool = False) -> bool:
    global _stopwords_tuples
    w = word.strip().lower()
    if not w or w in _stopwords_set:
        return False
    _data["stopwords"].append(w)
    _stopwords_set.add(w)
    _stopwords_tuples.append((w, w.lower(), _squeeze_simple(w.lower())))
    if hidden:
        _data.setdefault("hidden_words", []).append(w)
        _hidden_words_set.add(w)
        _save_kv("hidden_words")
    _save_kv("stopwords")
    save()
    return True

# --- варны ---

def get_warns(chat_id: int, user_id: int) -> int:
    return _data["warns"].get(_key(chat_id, user_id), 0)

def add_warn(chat_id: int, user_id: int) -> int:
    k = _key(chat_id, user_id)
    n = _data["warns"].get(k, 0) + 1
    _data["warns"][k] = n
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT INTO warns (chat_id, user_id, count) VALUES (?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count
        """, (chat_id, user_id, n))
        conn.commit()
    save()
    return n

def reset_warns(chat_id: int, user_id: int) -> None:
    _data["warns"].pop(_key(chat_id, user_id), None)
    conn = _get_connection()
    with _LOCK:
        conn.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit()
    save()

# --- белый список ссылок ---

def link_allowed(chat_id: int, user_id: int) -> bool:
    return _key(chat_id, user_id) in _link_whitelist_set

def allow_link(chat_id: int, user_id: int) -> bool:
    k = _key(chat_id, user_id)
    if k in _link_whitelist_set:
        return False
    _data["link_whitelist"].append(k)
    _link_whitelist_set.add(k)
    _save_kv("link_whitelist")
    save()
    return True

def disallow_link(chat_id: int, user_id: int) -> bool:
    k = _key(chat_id, user_id)
    if k in _link_whitelist_set or k in _data["link_whitelist"]:
        if k in _data["link_whitelist"]:
            _data["link_whitelist"].remove(k)
        _link_whitelist_set.discard(k)
        _save_kv("link_whitelist")
        save()
        return True
    return False

# --- доверенные пользователи (мимо всех проверок) ---

def is_trusted(chat_id: int, user_id: int) -> bool:
    return _key(chat_id, user_id) in _trusted_set

def toggle_trusted(chat_id: int, user_id: int) -> bool:
    """Вернёт True если добавили, False если убрали."""
    k = _key(chat_id, user_id)
    if k in _trusted_set or k in _data["trusted"]:
        if k in _data["trusted"]:
            _data["trusted"].remove(k)
        _trusted_set.discard(k)
        _save_kv("trusted")
        save()
        return False
    _data["trusted"].append(k)
    _trusted_set.add(k)
    _save_kv("trusted")
    save()
    return True

# --- флаги/числа/строки-оверрайды (поверх config, меняются в рантайме) ---

def get_flag(name: str, default: bool) -> bool:
    return bool(_data["flags"].get(name, default))

def set_flag(name: str, value: bool) -> None:
    _data["flags"][name] = bool(value)
    _save_kv("flags")
    save()

def get_num(name: str, default: int) -> int:
    return int(_data["nums"].get(name, default))

def set_num(name: str, value: int) -> None:
    _data["nums"][name] = int(value)
    _save_kv("nums")
    save()

def get_str(name: str, default: str) -> str:
    return str(_data["strs"].get(name, default))

def set_str(name: str, value: str) -> None:
    _data["strs"][name] = str(value)
    _save_kv("strs")
    save()

# --- правила группы ---

def get_rules() -> str:
    return _data.get("rules", "")

def set_rules(text: str) -> None:
    _data["rules"] = text
    _save_kv("rules")
    save()

# --- статистика (переживает перезапуск) ---

def load_stats() -> dict:
    return dict(_data.get("stats", {}))

def save_stats(stats: dict) -> None:
    _data["stats"] = dict(stats)
    _save_kv("stats")
    save()

# --- триггеры / автоответы ---

def triggers() -> dict:
    return _data.setdefault("triggers", {})

def add_trigger(key: str, reply: str) -> None:
    triggers()[key.strip().lower()] = reply
    _save_kv("triggers")
    save()

def del_trigger(key: str) -> bool:
    if key.strip().lower() in triggers():
        del triggers()[key.strip().lower()]
        _save_kv("triggers")
        save()
        return True
    return False

def mod_number(user_id: int) -> int:
    """Стабильный номер админа для анонимного режима («Модератор #N»)."""
    m = _data.setdefault("mod_numbers", {})
    k = str(user_id)
    if k not in m:
        m[k] = (max(m.values()) + 1) if m else 1
        _save_kv("mod_numbers")
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
    _save_kv("roles")
    save()

def roles_all() -> dict:
    return _data.setdefault("roles", {})

# --- оверрайд набора прав по ролям (редактируется из панели) ---

def get_role_perms_override(role: str) -> list | None:
    """Список прав роли, если он переопределён из панели, иначе None (=дефолт config)."""
    return _data.setdefault("role_perms", {}).get(role)

def set_role_perms(role: str, perms: list) -> None:
    _data.setdefault("role_perms", {})[role] = sorted(set(perms))
    _save_kv("role_perms")
    save()

def role_perms_all() -> dict:
    return _data.setdefault("role_perms", {})

# --- пер-юзерный оверрайд прав (лично человеку, поверх его роли) ---

def get_user_perms_override(user_id: int) -> list | None:
    """Личный набор прав юзера, если задан из панели, иначе None (=права роли)."""
    return _data.setdefault("user_perms", {}).get(str(user_id))

def set_user_perms(user_id: int, perms: list | None) -> None:
    """Задать личный набор прав (перебивает роль). None — сбросить к правам роли."""
    up = _data.setdefault("user_perms", {})
    if perms is None:
        up.pop(str(user_id), None)
    else:
        up[str(user_id)] = sorted(set(perms))
    _save_kv("user_perms")
    save()

def user_perms_all() -> dict:
    return _data.setdefault("user_perms", {})

# --- кастомные названия рангов (только отображаемое имя; ключ роли не меняется) ---

def get_role_title(role: str) -> str | None:
    return _data.setdefault("role_titles", {}).get(role)

def set_role_title(role: str, title: str | None) -> None:
    t = _data.setdefault("role_titles", {})
    if title:
        t[role] = title
    else:
        t.pop(role, None)
    _save_kv("role_titles")
    save()

def role_titles_all() -> dict:
    return _data.setdefault("role_titles", {})

# --- владельцы (только они выдают роли/должности) ---

def root_owners() -> set:
    """Главные владельцы из config.OWNER_IDS — их не снять и не переопределить."""
    return {int(u) for u in getattr(config, "OWNER_IDS", ())}

def is_root_owner(user_id: int) -> bool:
    """Главный владелец бота (жёстко прописан в config) — неприкосновенен."""
    return int(user_id) in root_owners()

def owners_all() -> list:
    """Владельцы: главные (из config) + добавленные через панель."""
    o = _data.setdefault("owners", [])
    return sorted(root_owners() | set(o))

def is_owner(user_id: int) -> bool:
    uid = int(user_id)
    return uid in root_owners() or uid in _owners_set

def add_owner(user_id: int) -> bool:
    """True — добавили, False — уже был владельцем."""
    uid = int(user_id)
    o = _data.setdefault("owners", [])
    if uid in root_owners() or uid in _owners_set or uid in o:
        return False
    o.append(uid)
    _owners_set.add(uid)
    _save_kv("owners")
    save()
    return True

def remove_owner(user_id: int) -> bool:
    """True — сняли, False — не был владельцем (главного владельца снять нельзя)."""
    uid = int(user_id)
    if uid in root_owners():
        return False
    o = _data.setdefault("owners", [])
    if uid in _owners_set or uid in o:
        if uid in o:
            o.remove(uid)
        _owners_set.discard(uid)
        _save_kv("owners")
        save()
        return True
    return False

# --- награды (/наградить): свободный текст, по желанию с награждающим и датой ---

def awards_of(user_id: int) -> list:
    """Список наград юзера (свежие в конце)."""
    return _data.setdefault("awards", {}).get(str(int(user_id)), [])

def is_awarded(user_id: int) -> bool:
    """True — у юзера есть хотя бы одна награда («награждён»)."""
    return bool(awards_of(user_id))

def add_award(user_id: int, text: str, by_id: int = 0, by_name: str = "") -> dict:
    """Выдать награду. Текст сохраняется как есть — с пробелами и любыми символами."""
    uid = int(user_id)
    rec = {"text": (text or "").strip(), "by": int(by_id or 0), "by_name": by_name or "",
           "ts": datetime.now().strftime("%d.%m.%Y %H:%M")}
    _data.setdefault("awards", {}).setdefault(str(uid), []).append(rec)
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT INTO awards (user_id, text, by_id, by_name, ts)
        VALUES (?, ?, ?, ?, ?)
        """, (uid, rec["text"], rec["by"], rec["by_name"], rec["ts"]))
        conn.commit()
    save()
    return rec

def del_award(user_id: int, index: int) -> dict | None:
    """Убрать награду по номеру в списке (1-based). None — если такой нет."""
    uid = int(user_id)
    a = _data.setdefault("awards", {}).get(str(uid), [])
    if not (1 <= index <= len(a)):
        return None
    rec = a.pop(index - 1)
    conn = _get_connection()
    with _LOCK:
        conn.execute("DELETE FROM awards WHERE user_id=?", (uid,))
        for item in a:
            conn.execute("""
            INSERT INTO awards (user_id, text, by_id, by_name, ts)
            VALUES (?, ?, ?, ?, ?)
            """, (uid, item.get("text", ""), item.get("by", 0), item.get("by_name", ""), item.get("ts", "")))
        conn.commit()
    save()
    return rec

# --- белый список стикерпаков (не проверять на 18+) ---

def _norm_pack(set_name: str) -> str:
    return (set_name or "").strip().lower()

def sticker_packs() -> list:
    return _data.setdefault("sticker_packs", [])

def is_pack_allowed(set_name: str) -> bool:
    """True — стикерпак в белом списке (пропускать без NSFW-проверки)."""
    name = _norm_pack(set_name)
    return bool(name) and name in _sticker_packs_set

def allow_pack(set_name: str) -> bool:
    """True — добавили в белый список, False — уже был там (или пустое имя)."""
    name = _norm_pack(set_name)
    if not name:
        return False
    p = _data.setdefault("sticker_packs", [])
    if name in _sticker_packs_set or name in p:
        return False
    p.append(name)
    _sticker_packs_set.add(name)
    _save_kv("sticker_packs")
    save()
    return True

def disallow_pack(set_name: str) -> bool:
    """True — убрали из белого списка, False — его там не было."""
    name = _norm_pack(set_name)
    p = _data.setdefault("sticker_packs", [])
    if name in _sticker_packs_set or name in p:
        if name in p:
            p.remove(name)
        _sticker_packs_set.discard(name)
        _save_kv("sticker_packs")
        save()
        return True
    return False

# --- верификация по номеру телефона (глобально, по user_id) ---

def is_phone_verified(user_id: int) -> bool:
    return str(user_id) in _data.setdefault("verified_phones", {})

def set_phone_verified(user_id: int, prefix: str, tail: str, ts: str = "") -> None:
    """Пометить юзера прошедшим верификацию. Полный номер НЕ храним —
    только код страны (prefix) и 2 последние цифры (tail) для справки админу."""
    uid = int(user_id)
    _data.setdefault("verified_phones", {})[str(uid)] = {
        "prefix": prefix, "tail": tail, "ts": ts,
    }
    _data.setdefault("pending_verify", {}).pop(str(uid), None)
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO verified_phones (user_id, prefix, tail, ts)
        VALUES (?, ?, ?, ?)
        """, (uid, prefix, tail, ts))
        conn.execute("DELETE FROM pending_verify WHERE user_id=?", (uid,))
        conn.commit()
    save()

def unverify_phone(user_id: int) -> bool:
    """Снять верификацию (напр. если админ хочет заставить перепройти)."""
    uid = int(user_id)
    if str(uid) in _data.setdefault("verified_phones", {}):
        del _data["verified_phones"][str(uid)]
        conn = _get_connection()
        with _LOCK:
            conn.execute("DELETE FROM verified_phones WHERE user_id=?", (uid,))
            conn.commit()
        save()
        return True
    return False

def verified_count() -> int:
    return len(_data.setdefault("verified_phones", {}))

# --- ожидающие подтверждения номера (переживает перезапуск) ---

def set_pending_verify(user_id: int, chat_id: int, notice_id=None, ts: str = "") -> None:
    uid = int(user_id)
    _data.setdefault("pending_verify", {})[str(uid)] = {
        "chat": chat_id, "notice": notice_id, "ts": ts,
    }
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO pending_verify (user_id, chat, notice, prefix, tail, review, ts)
        VALUES (?, ?, ?, '', '', 0, ?)
        """, (uid, chat_id, notice_id or 0, ts))
        conn.commit()
    save()

def set_pending_review(user_id: int, chat_id: int, notice_id, prefix: str,
                       tail: str, ts: str = "") -> None:
    """Как pending_verify, но с данными для ручного одобрения (код/2 цифры)."""
    uid = int(user_id)
    _data.setdefault("pending_verify", {})[str(uid)] = {
        "chat": chat_id, "notice": notice_id, "prefix": prefix, "tail": tail,
        "review": True, "ts": ts,
    }
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO pending_verify (user_id, chat, notice, prefix, tail, review, ts)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (uid, chat_id, notice_id or 0, prefix, tail, ts))
        conn.commit()
    save()

def get_pending_verify(user_id: int) -> dict | None:
    return _data.setdefault("pending_verify", {}).get(str(user_id))

def clear_pending_verify(user_id: int) -> None:
    uid = int(user_id)
    if str(uid) in _data.setdefault("pending_verify", {}):
        del _data["pending_verify"][str(uid)]
        conn = _get_connection()
        with _LOCK:
            conn.execute("DELETE FROM pending_verify WHERE user_id=?", (uid,))
            conn.commit()
        save()

# --- журнал действий (audit log) ---

def add_audit(entry: dict) -> None:
    log = _data.setdefault("audit", [])
    log.append(entry)
    del log[:-AUDIT_LIMIT]  # держим только последние AUDIT_LIMIT
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT INTO audit (ts, actor, action, target_id, target_name, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (entry.get("ts", ""), entry.get("actor", ""), entry.get("action", ""),
              entry.get("target_id", 0), entry.get("target_name", ""), entry.get("reason", "")))
        conn.execute("""
        DELETE FROM audit WHERE id NOT IN (
            SELECT id FROM audit ORDER BY id DESC LIMIT ?
        )
        """, (AUDIT_LIMIT,))
        conn.commit()
    save()

def get_audit(n: int = 15) -> list[dict]:
    return list(reversed(_data.get("audit", [])[-n:]))

def get_audit_for(target_id: int, n: int = 20) -> list[dict]:
    """История действий по конкретному пользователю (свежие сверху)."""
    hits = [e for e in _data.get("audit", []) if str(e.get("target_id")) == str(target_id)]
    return list(reversed(hits[-n:]))

# --- активность участников (персистится; видна админу в /info и в бэкапе) ---

def activity_all() -> dict:
    return _data.setdefault("activity", {})

def get_activity(chat_id: int, user_id: int) -> dict:
    return activity_all().get(_key(chat_id, user_id), {})

def bump_activity(chat_id: int, user_id: int, ts_iso: str, add: int = 1,
                  do_save: bool = False) -> None:
    """Инкремент счётчика сообщений в SQLite и в памяти."""
    k = _key(chat_id, user_id)
    rec = activity_all().setdefault(k, {})
    if not rec.get("first_seen"):
        rec["first_seen"] = ts_iso
    rec["msgs"] = int(rec.get("msgs", 0)) + add
    rec["last_seen"] = ts_iso
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT INTO activity (chat_id, user_id, first_seen, msgs, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            msgs = msgs + ?,
            last_seen = ?
        """, (chat_id, user_id, ts_iso, add, ts_iso, add, ts_iso))
        conn.commit()
    if do_save:
        save()

def set_activity_msgs(chat_id: int, user_id: int, msgs: int, ts_iso: str) -> None:
    """Переписать счётчик (для flush: base + сессия)."""
    k = _key(chat_id, user_id)
    rec = activity_all().setdefault(k, {"msgs": 0})
    rec["msgs"] = msgs
    rec["last_seen"] = ts_iso
    rec.setdefault("first_seen", ts_iso)
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT INTO activity (chat_id, user_id, first_seen, msgs, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            msgs = ?,
            last_seen = ?
        """, (chat_id, user_id, ts_iso, msgs, ts_iso, msgs, ts_iso))
        conn.commit()

# --- репутация (+реп / -реп), пер-чат ---

def _rep_all() -> dict:
    return _data.setdefault("reputation", {})

def get_rep(chat_id: int, user_id: int) -> dict:
    """Счётчик репутации юзера в чате: {"score", "plus", "minus"}."""
    rec = _rep_all().get(_key(chat_id, user_id)) or {}
    return {"score": int(rec.get("score", 0)),
            "plus": int(rec.get("plus", 0)),
            "minus": int(rec.get("minus", 0))}

def add_rep(chat_id: int, target_id: int, delta: int) -> dict:
    """Начислить +1/-1 получателю. Возвращает новый счётчик."""
    return change_rep(chat_id, target_id, 1 if delta > 0 else -1)

def change_rep(chat_id: int, target_id: int, delta: int) -> dict:
    """Изменить репутацию получателя на delta (+/- N). Возвращает новый счётчик."""
    k = _key(chat_id, target_id)
    rec = _rep_all().setdefault(k, {"score": 0, "plus": 0, "minus": 0})
    rec["score"] = int(rec.get("score", 0)) + delta
    if delta > 0:
        rec["plus"] = int(rec.get("plus", 0)) + delta
    elif delta < 0:
        rec["minus"] = int(rec.get("minus", 0)) + abs(delta)
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO reputation (chat_id, user_id, score, plus, minus)
        VALUES (?, ?, ?, ?, ?)
        """, (chat_id, target_id, rec["score"], rec["plus"], rec["minus"]))
        conn.commit()
    save()
    return {"score": rec["score"], "plus": rec["plus"], "minus": rec["minus"]}

# --- суточная квота выдач репутации (сброс в 00:00 МСК, день считает bot.py) ---

def rep_quota(chat_id: int, giver_id: int, day: str) -> dict:
    """Сколько выдач сделал giver за указанный день и кому: {"count", "targets"}.

    Если сохранённый день не совпадает с текущим (day) — считаем квоту нулевой
    (наступили новые сутки, старый счётчик уже неактуален).
    """
    rec = _data.setdefault("rep_quota", {}).get(_key(chat_id, giver_id))
    if not rec or rec.get("day") != day:
        return {"count": 0, "targets": []}
    return {"count": int(rec.get("count", 0)), "targets": list(rec.get("targets", []))}

def rep_quota_add(chat_id: int, giver_id: int, target_id: int, day: str) -> dict:
    """Учесть выдачу в суточной квоте giver'а. Возвращает {"count", "targets"}."""
    rq = _data.setdefault("rep_quota", {})
    k = _key(chat_id, giver_id)
    rec = rq.get(k)
    if not rec or rec.get("day") != day:      # новые сутки -> обнуляем
        rec = {"day": day, "count": 0, "targets": []}
    rec["count"] = int(rec.get("count", 0)) + 1
    if target_id not in rec["targets"]:
        rec["targets"].append(target_id)
    rq[k] = rec
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO rep_quota (chat_id, giver_id, day, count, targets_json)
        VALUES (?, ?, ?, ?, ?)
        """, (chat_id, giver_id, rec["day"], rec["count"], json.dumps(rec["targets"])))
        conn.commit()
    save()
    return {"count": rec["count"], "targets": list(rec["targets"])}

def rep_top(chat_id: int, n: int = 10) -> list:
    """Топ по репутации в чате: [(user_id, rec), ...] по убыванию score."""
    prefix = f"{chat_id}:"
    items = []
    for k, rec in _rep_all().items():
        if not k.startswith(prefix):
            continue
        try:
            uid = int(k.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        items.append((uid, {"score": int(rec.get("score", 0)),
                            "plus": int(rec.get("plus", 0)),
                            "minus": int(rec.get("minus", 0))}))
    items.sort(key=lambda x: x[1]["score"], reverse=True)
    return items[:n]

# --------------------------------------------------- штраф за флуд

def set_flood_penalty(chat_id: int, user_id: int, until_iso: str) -> None:
    """Установить штраф за флуд (все сообщения удаляются)."""
    _data.setdefault("flood_penalties", {})[_key(chat_id, user_id)] = until_iso
    conn = _get_connection()
    with _LOCK:
        conn.execute("""
        INSERT OR REPLACE INTO flood_penalties (chat_id, user_id, until_iso)
        VALUES (?, ?, ?)
        """, (chat_id, user_id, until_iso))
        conn.commit()
    save()

def get_flood_penalty(chat_id: int, user_id: int) -> str | None:
    """Получить ISO-время окончания штрафа за флуд."""
    return _data.get("flood_penalties", {}).get(_key(chat_id, user_id))

def is_flood_penalized(chat_id: int, user_id: int) -> bool:
    """Проверить, действует ли штраф за флуд. Если истёк — автоматически очистить."""
    ts = get_flood_penalty(chat_id, user_id)
    if not ts:
        return False
    try:
        until = datetime.fromisoformat(ts)
        now_dt = datetime.now(until.tzinfo) if until.tzinfo else datetime.utcnow()
        if now_dt < until:
            return True
        clear_flood_penalty(chat_id, user_id)
        return False
    except Exception:
        return False

def clear_flood_penalty(chat_id: int, user_id: int) -> None:
    """Снять штраф за флуд."""
    fp = _data.get("flood_penalties")
    if fp and _key(chat_id, user_id) in fp:
        fp.pop(_key(chat_id, user_id), None)
        conn = _get_connection()
        with _LOCK:
            conn.execute("DELETE FROM flood_penalties WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            conn.commit()
        save()

# --------------------------------------------------- база @никнеймов

def save_username(username: str, user_id: int, full_name: str = "") -> None:
    """Запомнить связку username <-> user_id в персистентное хранилище (переживает перезапуск)."""
    if not username or not user_id:
        return
    uname = str(username).lstrip("@").strip().lower()
    if not uname:
        return
    unames = _data.setdefault("usernames", {})
    u2n = _data.setdefault("user_to_uname", {})
    uid_str = str(user_id)
    uid_int = int(user_id)
    old_uname = u2n.get(uid_str)
    changed = False
    conn = _get_connection()
    with _LOCK:
        if old_uname and old_uname != uname:
            unames.pop(old_uname, None)
            conn.execute("DELETE FROM usernames WHERE username=?", (old_uname,))
            changed = True
        if unames.get(uname) != uid_int or u2n.get(uid_str) != uname:
            unames[uname] = uid_int
            u2n[uid_str] = uname
            conn.execute("INSERT OR REPLACE INTO usernames (username, user_id) VALUES (?, ?)", (uname, uid_int))
            conn.execute("INSERT OR REPLACE INTO user_to_uname (user_id, username) VALUES (?, ?)", (uid_int, uname))
            changed = True
        if changed:
            conn.commit()
    if changed:
        save()

def resolve_username(username: str) -> int | None:
    """Найти user_id по @никнейму (без учёта регистра)."""
    if not username:
        return None
    uname = str(username).lstrip("@").strip().lower()
    return _data.setdefault("usernames", {}).get(uname)

def get_user_username(user_id: int) -> str | None:
    """Найти последний известный @никнейм пользователя по его user_id."""
    return _data.setdefault("user_to_uname", {}).get(str(user_id))

def get_all_usernames() -> dict[str, int]:
    """Все известные боту @никнеймы: {username.lower(): user_id}."""
    return dict(_data.setdefault("usernames", {}))
