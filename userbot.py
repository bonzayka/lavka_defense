# -*- coding: utf-8 -*-
"""
Опциональный юзербот на Telethon для ПОЛНОГО скана участников группы.

Зачем: Bot API не умеет перечислять всех участников, поэтому чистый бот кикает
только «виденных» удалённых. Юзербот с ТВОЕЙ user-сессией проходит по всему
списку участников и выкидывает все удалённые аккаунты разом.

Всё опционально и под защитой импорта: если Telethon не установлен или сессия
не настроена — available()==False, и бот работает как раньше (Bot-API-скан).

Настройка (config.py / env):
    USERBOT_ENABLED = True
    USERBOT_API_ID / TG_API_ID       — api_id с my.telegram.org
    USERBOT_API_HASH / TG_API_HASH   — api_hash оттуда же
    USERBOT_SESSION / TG_SESSION     — путь к .session

Первый вход (один раз, интерактивно на машине с ботом):
    venv\\Scripts\\python.exe userbot.py login
"""

from __future__ import annotations

import asyncio
import logging

import config

log = logging.getLogger("antispam.userbot")

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError
    _HAS_TELETHON = True
except Exception:                       # Telethon не установлен — это норм
    TelegramClient = None               # type: ignore
    RPCError = Exception                # type: ignore
    _HAS_TELETHON = False


def _cfg(name: str, default=None):
    return getattr(config, name, default)


def available() -> bool:
    """Готов ли юзербот к работе (есть Telethon + api_id/hash + включён)."""
    return bool(
        _HAS_TELETHON
        and _cfg("USERBOT_ENABLED", False)
        and _cfg("USERBOT_API_ID", 0)
        and _cfg("USERBOT_API_HASH", "")
    )


def status() -> str:
    if not _HAS_TELETHON:
        return "не установлен (pip install telethon)"
    if not _cfg("USERBOT_ENABLED", False):
        return "выключен (USERBOT_ENABLED=False)"
    if not (_cfg("USERBOT_API_ID", 0) and _cfg("USERBOT_API_HASH", "")):
        return "нет api_id/api_hash"
    return "готов"


def _is_deleted(user) -> bool:
    if user is None:
        return False
    if user.__class__.__name__ == "UserEmpty":
        return True
    return bool(getattr(user, "deleted", False))


async def _client():
    return TelegramClient(
        _cfg("USERBOT_SESSION", "userbot.session"),
        int(_cfg("USERBOT_API_ID", 0)),
        str(_cfg("USERBOT_API_HASH", "")),
    )


async def scan_deleted(chat, *, kick: bool = True, limit: int | None = None) -> dict:
    """Пройтись по ВСЕМ участникам chat и (опц.) кикнуть удалённые аккаунты.

    chat — id/username группы (юзербот должен состоять в ней и иметь право
    удалять участников). Возвращает {'scanned','deleted','kicked','error'}.
    """
    res = {"scanned": 0, "deleted": 0, "kicked": 0, "error": None}
    if not available():
        res["error"] = f"userbot недоступен: {status()}"
        return res
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights

    client = await _client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            res["error"] = "user-сессия не авторизована (запусти: python userbot.py login)"
            return res
        entity = await client.get_entity(chat)
        async for user in client.iter_participants(entity, aggressive=True):
            res["scanned"] += 1
            if not _is_deleted(user):
                continue
            res["deleted"] += 1
            if kick:
                try:
                    await client(EditBannedRequest(
                        entity, user.id,
                        ChatBannedRights(until_date=None, view_messages=True)))
                    res["kicked"] += 1
                    await asyncio.sleep(0.4)      # не ловить флуд-лимит
                except RPCError as e:
                    log.warning("userbot: не смог кикнуть %s: %s", user.id, e)
            if limit and res["deleted"] >= limit:
                break
    except Exception as e:                        # noqa: BLE001
        res["error"] = str(e)
        log.warning("userbot scan_deleted упал: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return res


async def _login():
    """Интерактивный первый вход — создаёт .session один раз."""
    if not _HAS_TELETHON:
        print("Telethon не установлен: venv\\Scripts\\python.exe -m pip install telethon")
        return
    client = await _client()
    await client.start()                          # спросит телефон + код
    me = await client.get_me()
    print(f"OK, вошёл как: {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        asyncio.run(_login())
    else:
        print("Статус юзербота:", status())
        print("Вход:  venv\\Scripts\\python.exe userbot.py login")
