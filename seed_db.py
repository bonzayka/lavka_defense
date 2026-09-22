# -*- coding: utf-8 -*-
"""Скрипт переноса и посева базы данных в SQLite."""
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE, "data.json")
DB_PATH = os.path.join(BASE, "data.db")

def init_sqlite_schema(conn: sqlite3.Connection):
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
    # Ensure extra columns exist if table was created previously without them
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

print("Schema helper defined.")
