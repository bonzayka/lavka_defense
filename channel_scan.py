# -*- coding: utf-8 -*-
"""
Деанон-скан каналов/чатов через юзербот (Telethon).

Идея: обычный участник кидает в чат ссылку t.me/... или @username канала. Бот
через ТВОЙ user-аккаунт заходит в этот канал, читает последние посты и проверяет
их на чужие персональные данные (deanon.is_deanon) и стоп-слова (textguard).
Если канал — рассадник деанона/запрещёнки, автора ссылки наказываем.

Всё опционально и под защитой импорта: без Telethon/сессии available()==False и
бот работает как раньше. Логика намеренно консервативна:
  • сканируем ТОЛЬКО каналы/чаты (не пользователей-людей);
  • крупные паблики (>= DEANON_CHANNEL_MAX участников) пропускаем — это новостники,
    а не сливные помойки; лишний раз аккаунт не светим;
  • вердикт по каналу кэшируется (TTL) и между сканами держим паузу (rate-limit),
    чтобы беречь аккаунт от флуд-лимитов и банов за автоматизацию.

⚠️ Автоматизированное чтение чужих каналов пользовательским аккаунтом Telegram
может привести к ограничению/бану аккаунта. Включать осознанно (DEANON_CHANNEL_ENABLED),
на расходном аккаунте.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import config
import deanon
import textguard
import storage
import userbot

log = logging.getLogger("antispam.chanscan")

# t.me/NAME, telegram.me/NAME, @NAME, https://t.me/NAME/123 (пост) — вытаскиваем NAME.
# Пропускаем служебные пути (joinchat/+инвайты, addstickers, share и пр.) — их не резолвим.
_TME_RE = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:s/)?(?P<name>[A-Za-z][\w\d_]{3,31})",
    re.IGNORECASE,
)
_AT_RE = re.compile(r"(?<![\w@])@(?P<name>[A-Za-z][\w\d_]{3,31})")

# Пути t.me, которые НЕ являются публичным юзернеймом канала.
_SKIP_NAMES = {
    "joinchat", "addstickers", "addemoji", "share", "proxy", "socks",
    "setlanguage", "confirmphone", "login", "bg", "c", "iv", "s",
}

# Кэш вердиктов по цели: name/id -> (verdict_dict, ts).
_cache: dict[str, tuple[dict, float]] = {}
_last_scan_at = 0.0
_scan_lock = asyncio.Lock()


def available() -> bool:
    """Готов ли скан каналов (включён + юзербот доступен)."""
    return bool(getattr(config, "DEANON_CHANNEL_ENABLED", False) and userbot.available())


def status() -> str:
    if not getattr(config, "DEANON_CHANNEL_ENABLED", False):
        return "выключен (DEANON_CHANNEL_ENABLED=0)"
    if not userbot.available():
        return f"нужен юзербот: {userbot.status()}"
    return "готов"


def extract_targets(text: str) -> list[str]:
    """Достать из текста имена каналов (t.me/NAME и @NAME), без дублей и служебных."""
    if not text:
        return []
    out, seen = [], set()
    for m in _TME_RE.finditer(text):
        name = m.group("name")
        low = name.lower()
        if low in _SKIP_NAMES or low in seen:
            continue
        seen.add(low)
        out.append(name)
    for m in _AT_RE.finditer(text):
        name = m.group("name")
        low = name.lower()
        if low in _SKIP_NAMES or low in seen:
            continue
        # bot-юзернеймы (…bot) не сканируем — это боты, не каналы-помойки.
        if low.endswith("bot"):
            continue
        seen.add(low)
        out.append(name)
    return out


def _scan_texts(texts: list[str]) -> dict:
    """Прогнать список текстов постов через деанон + стоп-слова. Вернуть вердикт."""
    min_hits = int(getattr(config, "DEANON_CHANNEL_MIN_HITS", 2))
    stop = storage.stopwords()
    for t in texts:
        if not t:
            continue
        hit, types = deanon.is_deanon(t, min_hits=min_hits)
        if hit:
            return {"hit": True, "reason": "ПДн в постах: " + ", ".join(types)}
        sw = textguard.find_stopword(t, stop)
        if sw:
            hidden = storage.is_hidden_word(sw)
            reason = "стоп-слово в постах" if hidden else f"стоп-слово в постах: {sw}"
            return {"hit": True, "reason": reason}
    return {"hit": False, "reason": ""}


async def scan_target(name: str) -> dict:
    """Зарезолвить канал по имени и просканировать его посты.

    Возвращает dict:
      {'hit': bool, 'reason': str, 'title': str, 'count': int}  — просканировано,
      {'skip': True, 'why': str}                                — пропущено (не канал/крупный/личка),
      {'error': str}                                            — сбой резолва/чтения.
    """
    if not available():
        return {"skip": True, "why": f"scan недоступен: {status()}"}

    key = name.lower().lstrip("@")
    ttl = float(getattr(config, "DEANON_CHANNEL_CACHE_TTL", 3600))
    cached = _cache.get(key)
    if cached and (time.time() - cached[1]) < ttl:
        return cached[0]

    global _last_scan_at
    async with _scan_lock:
        # rate-limit: не чаще, чем раз в DEANON_CHANNEL_RATE секунд.
        gap = float(getattr(config, "DEANON_CHANNEL_RATE", 3.0))
        wait = gap - (time.time() - _last_scan_at)
        if wait > 0:
            await asyncio.sleep(wait)
        result = await _do_scan(key)
        _last_scan_at = time.time()

    # кэшируем только окончательные вердикты (hit/skip), не транзиентные ошибки
    if "error" not in result:
        _cache[key] = (result, time.time())
    return result


async def _do_scan(name: str) -> dict:
    from telethon.tl.types import Channel, Chat

    max_members = int(getattr(config, "DEANON_CHANNEL_MAX", 100))
    msg_limit = int(getattr(config, "DEANON_CHANNEL_MSGS", 50))

    client = await userbot._client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"error": "user-сессия не авторизована"}
        try:
            entity = await client.get_entity(name)
        except Exception as e:                       # noqa: BLE001 — нет такого/приватный
            return {"skip": True, "why": f"не резолвится: {e}"}

        # Только каналы/группы. Пользователи-люди — не наша цель (не деанон-скан людей).
        if not isinstance(entity, (Channel, Chat)):
            return {"skip": True, "why": "не канал/чат"}

        title = getattr(entity, "title", name)
        try:
            full = await client.get_entity(entity)
            count = getattr(full, "participants_count", None) or 0
        except Exception:
            count = 0
        if count and count >= max_members:
            return {"skip": True, "why": f"крупный ({count} уч.)", "title": title}

        texts, n = [], 0
        try:
            async for msg in client.iter_messages(entity, limit=msg_limit):
                n += 1
                txt = getattr(msg, "message", None) or getattr(msg, "raw_text", None)
                if txt:
                    texts.append(txt)
        except Exception as e:                       # noqa: BLE001 — нет доступа к истории
            return {"skip": True, "why": f"история недоступна: {e}", "title": title}

        verdict = _scan_texts(texts)
        verdict["title"] = title
        verdict["count"] = n
        return verdict
    except Exception as e:                            # noqa: BLE001
        log.warning("channel_scan: скан %s упал: %s", name, e)
        return {"error": str(e)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def clear_cache() -> None:
    _cache.clear()
