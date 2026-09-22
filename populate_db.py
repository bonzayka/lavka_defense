# -*- coding: utf-8 -*-
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE, "data.json")
DB_PATH = os.path.join(BASE, "data.db")

from seed_db import init_sqlite_schema

def import_all_to_sqlite(data: dict, db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    init_sqlite_schema(conn)
    cur = conn.cursor()

    # 1. Activity
    for k, v in data.get("activity", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            cid, uid = int(parts[0]), int(parts[1])
            cur.execute("""
            INSERT OR REPLACE INTO activity (chat_id, user_id, first_seen, msgs, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """, (cid, uid, v.get("first_seen", ""), v.get("msgs", 0), v.get("last_seen", "")))

    # 2. Usernames
    for uname, uid in data.get("usernames", {}).items():
        clean_uname = uname.lstrip("@").strip().lower()
        cur.execute("INSERT OR REPLACE INTO usernames (username, user_id) VALUES (?, ?)", (clean_uname, int(uid)))
    for uid, uname in data.get("user_to_uname", {}).items():
        clean_uname = uname.lstrip("@").strip().lower()
        cur.execute("INSERT OR REPLACE INTO user_to_uname (user_id, username) VALUES (?, ?)", (int(uid), clean_uname))

    # 3. Warns
    for k, count in data.get("warns", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            cid, uid = int(parts[0]), int(parts[1])
            cur.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, count) VALUES (?, ?, ?)", (cid, uid, count))

    # 4. Reputation
    for k, v in data.get("reputation", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            cid, uid = int(parts[0]), int(parts[1])
            cur.execute("""
            INSERT OR REPLACE INTO reputation (chat_id, user_id, score, plus, minus)
            VALUES (?, ?, ?, ?, ?)
            """, (cid, uid, v.get("score", 0), v.get("plus", 0), v.get("minus", 0)))

    # 5. Rep quota
    for k, v in data.get("rep_quota", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            cid, gid = int(parts[0]), int(parts[1])
            cur.execute("""
            INSERT OR REPLACE INTO rep_quota (chat_id, giver_id, day, count, targets_json)
            VALUES (?, ?, ?, ?, ?)
            """, (cid, gid, v.get("day", ""), v.get("count", 0), json.dumps(v.get("targets", []))))

    # 6. Awards
    cur.execute("DELETE FROM awards")
    for uid_str, awards_list in data.get("awards", {}).items():
        uid = int(uid_str)
        for a in awards_list:
            cur.execute("""
            INSERT INTO awards (user_id, text, by_id, by_name, ts)
            VALUES (?, ?, ?, ?, ?)
            """, (uid, a.get("text", ""), a.get("by", 0), a.get("by_name", ""), a.get("ts", "")))

    # 7. Audit
    cur.execute("DELETE FROM audit")
    for entry in data.get("audit", []):
        cur.execute("""
        INSERT INTO audit (ts, actor, action, target_id, target_name, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (entry.get("ts", ""), entry.get("actor", ""), entry.get("action", ""),
              entry.get("target_id", 0), entry.get("target_name", ""), entry.get("reason", "")))

    # 8. Verified phones & pending verify
    for uid_str, rec in data.get("verified_phones", {}).items():
        cur.execute("""
        INSERT OR REPLACE INTO verified_phones (user_id, prefix, tail, ts)
        VALUES (?, ?, ?, ?)
        """, (int(uid_str), rec.get("prefix", ""), rec.get("tail", ""), rec.get("ts", "")))
    for uid_str, rec in data.get("pending_verify", {}).items():
        cur.execute("""
        INSERT OR REPLACE INTO pending_verify (user_id, chat, notice, prefix, tail, review, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (int(uid_str), rec.get("chat", 0), rec.get("notice", 0), rec.get("prefix", ""),
              rec.get("tail", ""), 1 if rec.get("review") else 0, rec.get("ts", "")))


    # 9. Flood penalties
    for k, until in data.get("flood_penalties", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            cid, uid = int(parts[0]), int(parts[1])
            cur.execute("INSERT OR REPLACE INTO flood_penalties (chat_id, user_id, until_iso) VALUES (?, ?, ?)",
                        (cid, uid, str(until)))

    # 10. KV Store (settings, flags, nums, strs, rules, triggers, mod_numbers, roles, etc.)
    kv_keys = [
        "stopwords", "hidden_words", "link_whitelist", "trusted", "flags",
        "nums", "strs", "stats", "rules", "triggers", "mod_numbers", "roles",
        "role_perms", "role_titles", "owners", "sticker_packs", "user_perms"
    ]
    for k in kv_keys:
        val = data.get(k, [] if k in ("stopwords", "hidden_words", "link_whitelist", "trusted", "owners", "sticker_packs") else ("" if k == "rules" else {}))
        cur.execute("INSERT OR REPLACE INTO kv_store (key, value_json) VALUES (?, ?)", (k, json.dumps(val, ensure_ascii=False)))

    conn.commit()
    conn.close()
    print("Imported successfully to SQLite:", db_path)

if __name__ == '__main__':
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        d = json.load(f)
    import_all_to_sqlite(d)
