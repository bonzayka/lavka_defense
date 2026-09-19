# -*- coding: utf-8 -*-
"""
ЛОКАЛЬНЫЕ СЕКРЕТЫ — НЕ КОММИТИТЬ! Файл в .gitignore.
Ключи юзербота (Telethon) с my.telegram.org. Используются в config.py.
"""

# api_id / api_hash аккаунта-юзербота (my.telegram.org).
TG_API_ID = 12804901
TG_API_HASH = "aacdc6ef82c90f1187b628ea8dc73102"

# Путь к файлу сессии (создастся при первом входе: python userbot.py login).
TG_SESSION = "userbot.session"

# Ключ AI-модерации (aiguard.py) — шлюз OrcaRouter, OpenAI-совместимый API.
# Модель по умолчанию: deepseek/deepseek-v4-flash-free (см. config.AI_MODEL).
ORCA_API_KEY = "sk-orca-7PGp6SckssYt7817eYS47F9V2NEtnkCcHS3Ezud7C0G"
