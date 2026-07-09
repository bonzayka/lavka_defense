# -*- coding: utf-8 -*-
"""
Детекция и чистка удалённых аккаунтов («Deleted Account»).

Как отличить удалёнку по Bot API: у неё пустое имя, нет фамилии и нет
@username (Telegram затирает профиль при удалении). Живой аккаунт всегда
имеет непустое first_name.

Ограничение Bot API (честно): перечислить ВСЕХ участников группы нельзя.
Бот кикает только тех удалённых, кого «видел» — кто писал, вступал или попал
в кэш ников. Полный проход по всем участникам делает опциональный userbot.py
(Telethon, требует user-сессию).
"""

from __future__ import annotations


def is_deleted_account(user) -> bool:
    """True — профиль похож на удалённый аккаунт.

    user — объект с атрибутами first_name/last_name/username (aiogram User
    или простой namespace в тестах).
    """
    if user is None:
        return False
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    uname = (getattr(user, "username", "") or "").strip()
    # Удалёнка: ни имени, ни фамилии, ни ника.
    return not first and not last and not uname


# Статусы участника, при которых имеет смысл кикать (он ещё в чате).
# 'creator'/'administrator' не трогаем; 'left'/'kicked' уже вне чата.
KICKABLE_STATUSES = {"member", "restricted"}


def should_kick_member(member) -> bool:
    """True — этого участника (объект ChatMember) стоит кикнуть как удалёнку."""
    if member is None:
        return False
    status = getattr(member, "status", None)
    if status not in KICKABLE_STATUSES:
        return False
    return is_deleted_account(getattr(member, "user", None))
