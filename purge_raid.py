# -*- coding: utf-8 -*-
"""
ЗАЧИСТКА РЕЙДА по датацентру (DC) — отдельный MTProto-скрипт (НЕ часть бота).

Зачем отдельно: Bot API НЕ умеет перечислять участников группы. Userbot (твой
обычный аккаунт через MTProto) — умеет, и видит DC профильного фото напрямую.
Большинство рейд-ботов сидят на DC5 (Singapore) -> вычищаем их.

⚠️ ЭТО ЭВРИСТИКА. DC5 — это и реальные люди из Юго-Восточной Азии. Поэтому
сначала ОБЯЗАТЕЛЬНО прогон в режиме DRY_RUN=True (только покажет, сколько и кого),
оцени списком, и лишь потом ставь DRY_RUN=False.

Установка:
    pip install telethon
Получи api_id и api_hash: https://my.telegram.org -> API development tools.
Аккаунт, под которым логинишься, должен быть АДМИНОМ группы с правом банить.

Запуск:
    python purge_raid.py
"""

import asyncio

from telethon import TelegramClient, functions, types
from telethon.tl.types import ChatBannedRights

# ======================= НАСТРОЙКИ =======================
API_ID = 123456                      # с my.telegram.org
API_HASH = "xxxxxxxxxxxxxxxxxxxxxxxx"  # с my.telegram.org
CHAT = "@your_group"                 # @username группы или числовой id (-100...)
BLOCK_DC = {5}                       # какие DC чистим (5 = Singapore)
DRY_RUN = True                       # True = только показать; False = реально банить
BAN_NO_PHOTO = False                 # банить ли аккаунты вообще без фото (рискованно)
ACTION = "ban"                       # ban (нельзя вернуться) | kick (можно вернуться)
SLEEP = 0.5                          # пауза между действиями (защита от флуд-лимита)
# =========================================================

client = TelegramClient("purge_session", API_ID, API_HASH)

BAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=True)   # бан
UNBAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=False)  # снять (для kick)


async def main():
    await client.start()
    entity = await client.get_entity(CHAT)

    # собрать админов, чтобы не трогать
    admins = set()
    async for a in client.iter_participants(entity, filter=types.ChannelParticipantsAdmins):
        admins.add(a.id)

    total = checked = flagged = acted = 0
    targets = []
    async for user in client.iter_participants(entity, aggressive=True):
        total += 1
        if user.id in admins or user.bot:
            continue
        checked += 1
        dc = getattr(user.photo, "dc_id", None) if user.photo else None
        is_bad = (dc in BLOCK_DC) or (user.photo is None and BAN_NO_PHOTO)
        if not is_bad:
            continue
        flagged += 1
        uname = f"@{user.username}" if user.username else f"id{user.id}"
        targets.append((user, dc))
        print(f"[{'DRY' if DRY_RUN else ACTION.upper()}] {uname} DC={dc}")
        if not DRY_RUN:
            try:
                if ACTION == "ban":
                    await client(functions.channels.EditBannedRequest(entity, user, BAN_RIGHTS))
                else:  # kick = ban + unban
                    await client(functions.channels.EditBannedRequest(entity, user, BAN_RIGHTS))
                    await client(functions.channels.EditBannedRequest(entity, user, UNBAN_RIGHTS))
                acted += 1
            except Exception as e:
                print("  ! ошибка:", e)
            await asyncio.sleep(SLEEP)

    print("\n==== ИТОГ ====")
    print(f"Всего участников просмотрено: {total}")
    print(f"Проверено (не админы/не боты): {checked}")
    print(f"Подходит под фильтр DC{sorted(BLOCK_DC)}: {flagged}")
    if DRY_RUN:
        print("Это был DRY_RUN — никого не тронул. Оцени список выше.")
        print("Доволен? Поставь DRY_RUN=False и запусти снова.")
    else:
        print(f"Реально {ACTION}: {acted}")


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
