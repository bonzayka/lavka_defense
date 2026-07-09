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


def session_authorized() -> bool:
    """Проверить (без интерактива), авторизована ли сохранённая .session."""
    if not available():
        return False

    async def _check():
        client = await _client()
        try:
            await client.connect()
            return await client.is_user_authorized()
        except Exception:
            return False
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        return asyncio.get_event_loop().run_until_complete(_check())
    except RuntimeError:
        # Уже внутри работающего loop — вызывать из async-обёртки нельзя синхронно.
        return False


async def session_authorized_async() -> bool:
    """Асинхронная проверка авторизации сессии (для вызова из бота)."""
    if not available():
        return False
    client = await _client()
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def logout() -> str:
    """Разлогинить и удалить авторизацию сессии."""
    if not _HAS_TELETHON:
        return "Telethon не установлен."
    client = await _client()
    try:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
            return "🚪 Юзербот вышел из аккаунта, сессия очищена."
        return "Сессия и так не авторизована."
    except Exception as e:                        # noqa: BLE001
        return f"Не смог выйти: {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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


# --------- пошаговый логин из чата (для BotHost: интерактивной консоли нет) ----
# Держим живого клиента между сообщениями в state-словаре (в памяти бота).

async def login_start(phone: str) -> tuple[dict | None, str]:
    """Шаг 1: подключиться и запросить код на номер. Вернёт (state|None, текст)."""
    if not _HAS_TELETHON:
        return None, "Telethon не установлен на хосте (pip install telethon)."
    if not (_cfg("USERBOT_API_ID", 0) and _cfg("USERBOT_API_HASH", "")):
        return None, "Нет api_id/api_hash (config.USERBOT_* / secrets_local.py)."
    client = await _client()
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            return None, (f"Уже авторизован как {me.first_name} "
                          f"(@{me.username}). Сессия готова, вход не нужен.")
        sent = await client.send_code_request(phone)
        state = {"client": client, "phone": phone,
                 "hash": sent.phone_code_hash}
        return state, "Код отправлен в Telegram."
    except Exception as e:                        # noqa: BLE001
        try:
            await client.disconnect()
        except Exception:
            pass
        return None, f"Не смог запросить код: {e}"


async def login_code(state: dict, code: str) -> str:
    """Шаг 2: ввести код. Вернёт 'ok' | 'need_password' | 'ok:<имя>' | 'error:...'."""
    from telethon.errors import SessionPasswordNeededError
    client = state["client"]
    try:
        await client.sign_in(state["phone"], code, phone_code_hash=state["hash"])
    except SessionPasswordNeededError:
        return "need_password"
    except Exception as e:                        # noqa: BLE001
        return f"error:{e}"
    return await _login_finish(state)


async def login_password(state: dict, password: str) -> str:
    """Шаг 3 (если включён облачный пароль 2FA)."""
    client = state["client"]
    try:
        await client.sign_in(password=password)
    except Exception as e:                        # noqa: BLE001
        return f"error:{e}"
    return await _login_finish(state)


async def _login_finish(state: dict) -> str:
    client = state["client"]
    try:
        me = await client.get_me()
        name = f"{me.first_name} (@{me.username})" if me else "аккаунт"
    except Exception:
        name = "аккаунт"
    try:
        await client.disconnect()                 # сессия сохранится в .session
    except Exception:
        pass
    return f"ok:{name}"


async def login_cancel(state: dict) -> None:
    try:
        await state["client"].disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        asyncio.run(_login())
    else:
        print("Статус юзербота:", status())
        print("Вход:  venv\\Scripts\\python.exe userbot.py login")
