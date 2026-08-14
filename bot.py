# -*- coding: utf-8 -*-
"""
Антиспам-бот для Telegram-группы.

Кратко:
  • 3-факторная капча на входе (пример / вопрос / углы фигуры); админы пропускаются;
    имена вступающих проверяются на мат/стоп-слова.
  • Картинки в 2 слоя: хеш по базе photo/ + ViT-классификатор 18+.
  • Модерация сообщений: ссылки, пересылки, посты «от имени канала», .apk,
    премиум-эмодзи, антифлуд, антимат и стоп-слова (с фильтром подмены символов).
  • Наказания: delete / warn (с лимитом) / mute / ban; ночной и тихий режимы;
    приветствие; удаление сервисных сообщений.
  • Команды для админов: /spam /reload /stats /help /ping /ban /unban /mute
    /unmute /warn /unwarn /whitelist /addword /delword /words /night /quiet
    /antimat /settings.

Требования: бот — АДМИН группы (бан / ограничение / удаление сообщений),
Group Privacy выключен.
"""

import asyncio
import html
import io
import logging
import os
import random
import re
import tempfile
from collections import deque
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, BaseMiddleware, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    ChatPermissions,
    BufferedInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError

import config
import channel_scan
import dcguard
import deanon
import deleted
import gore
import manager
import nsfwvit
import riskscore
import storage
import textguard
import userbot

IS_CHILD = manager.is_child()  # дочерний бот не поднимает свой менеджер

_LOG_FMT = "%(asctime)s | %(levelname)s | %(message)s"
_handlers: list = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler
    # Лог рядом с данными бота (у дочерних — свой файл).
    _log_dir = os.path.dirname(os.environ.get("DATA_FILE") or __file__) or "."
    _log_path = os.path.join(_log_dir, "bot.log")
    _handlers.append(RotatingFileHandler(_log_path, maxBytes=2_000_000,
                                         backupCount=3, encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, handlers=_handlers)
log = logging.getLogger("antispam")

session = AiohttpSession(proxy=config.PROXY) if config.PROXY else None
bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=session,
)
dp = Dispatcher()

# --- состояние в памяти ---
pending: dict[tuple[int, int], dict] = {}          # ждут капчу
ref_hashes: list[tuple[str, int]] = []             # хеши спам-картинок
recent: dict[tuple[int, int], deque] = {}          # последние сообщения (для зачистки)
flagged: dict[tuple[int, int], datetime] = {}      # антидубль уведомлений
admins_cache: dict[int, tuple[set, datetime]] = {} # кэш админов
flood: dict[tuple[int, int], deque] = {}           # тайминги сообщений (антифлуд)
repeat: dict[tuple[int, int], list] = {}           # [последний_текст, счётчик] (анти-повтор)
trigger_cooldown: dict[int, datetime] = {}         # троттлинг автоответов по чату
accept_burst: dict[int, deque] = {}                # тайминги принятых заявок (антирейд автоприёма)
pending_requests: dict[int, set] = {}              # chat_id -> {user_id} висящих заявок (для /clearrequests)
votes: dict[int, dict] = {}                        # vote_id -> состояние голосования /vb
vote_seq = 0                                        # счётчик id голосований
night_notice: dict[int, datetime] = {}             # троттлинг уведомления ночного режима
newcomer: dict[tuple[int, int], datetime] = {}     # когда юзер вошёл (ограничение новичков)
raid_joins: dict[int, deque] = {}                  # тайминги входов (антирейд)
raid_until: dict[int, datetime] = {}               # до какого времени активен локдаун
report_cooldown: dict[tuple[int, int], datetime] = {}  # антиспам жалоб
child_restarts: dict[str, int] = {}                # watchdog: счётчик перезапусков
rights_alert: dict[int, datetime] = {}             # троттлинг алерта о потере прав
report_votes: dict[tuple[int, int], dict] = {}     # (chat, msg_id) -> {voters:set, ts, done}
report_times: dict[tuple[int, int], deque] = {}    # (chat, reporter) -> deque[dt] (лимит/час)
panel_auth: set[int] = set()                       # кто прошёл пароль панели
panel_state: dict[int, str] = {}                   # ожидание ввода в панели
panel_newbot: dict[int, dict] = {}                 # черновик создаваемого бота (токен+юзернейм)
ub_login: dict[int, dict] = {}                     # состояние скрытого логина юзербота (uid -> telethon state)
msgcount: dict[tuple[int, int], int] = {}          # счётчик сообщений юзеров (с момента старта)
dice_games: dict[int, dict] = {}                   # chat_id -> game state
uname_cache: dict[str, int] = {}                   # "@username" (lower, без @) -> user_id (для таргета по нику)
bot_self_id: int | None = None                     # id самого бота (для защиты цели), ставится в main()
bot_username: str | None = None                    # @username бота (для deep-link верификации), ставится в main()
verify_wait: dict[int, dict] = {}                  # user_id -> {chat_id, notice_id, joined, name, task} — ждут номер в ЛС
vb_cooldown: dict[tuple[int, int], datetime] = {}  # (chat, uid) -> когда не-персонал последний раз звал /vb
probation: dict[tuple[int, int], dict] = {}        # (chat, uid) -> {until, score, reasons} — новичок «под наблюдением»
known_chats: set[int] = set()                      # чаты, где бот видел активность (для свипа удалёнок)
last_deleted_sweep: datetime | None = None         # когда крутили автосвип удалёнок
crisis: dict[int, dict] = {}                        # chat_id -> состояние ЧС-режима (автопилот антирейда)
msg_wave: dict[int, deque] = {}                     # chat_id -> deque[(dt, uid)] для детекта волны сообщений
stats = {"challenged": 0, "passed": 0, "failed": 0, "img_muted": 0,
         "banned": 0, "reports": 0, "raids": 0, "risk_muted": 0, "deleted_kicked": 0,
         "crises": 0, "deanon_text": 0}

MUTE = ChatPermissions(can_send_messages=False)
# Режим фото-капчи: новичку можно ТОЛЬКО текст (ввести код), без медиа/ссылок.
TEXT_ONLY = ChatPermissions(
    can_send_messages=True, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_invite_users=False,
)
FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)
# Медиа-карантин новичка: можно ТОЛЬКО текст (никаких гиф/стикеров/фото/видео/
# аудио/голосовых/кружков/файлов/опросов). Ставится с until_date на NEWCOMER_MEDIA_HOURS.
# ВНИМАНИЕ: этот набор применяется ТОЛЬКО с use_independent_chat_permissions=True,
# иначе Telegram считает права зависимыми и can_add_web_page_previews=True
# автоматически включил бы обратно фото/видео/аудио/файлы. Поэтому превью тут тоже
# выключено — двойная страховка (см. grant_full_or_quarantine).
NEWCOMER_QUARANTINE = ChatPermissions(
    can_send_messages=True, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_invite_users=False,
)

SHAPES = {"треугольника": 3, "квадрата": 4, "пятиугольника": 5, "шестиугольника": 6}
COMMONSENSE = [
    ("Назови первую букву русского алфавита:", "А", ["Б", "Я", "О", "Д", "Ж"]),
    ("Сколько дней в неделе?", "7", ["5", "6", "8", "9"]),
    ("Какого цвета снег?", "Белый", ["Чёрный", "Синий", "Красный", "Зелёный"]),
    ("Столица России?", "Москва", ["Киев", "Минск", "Сочи", "Питер"]),
    ("Сколько пальцев на одной руке?", "5", ["3", "4", "6", "10"]),
    ("Сколько ног у кошки?", "4", ["2", "3", "6", "8"]),
    ("Сколько будет 2 + 2?", "4", ["3", "5", "6", "22"]),
    ("Какое время года самое холодное?", "Зима", ["Лето", "Весна", "Осень"]),
]
MAX_DOC_BYTES = 10 * 1024 * 1024
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|tg://|telega\.ph|teletype\.in)", re.I)


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def mention(user) -> str:
    name = (user.full_name or "пользователь").strip() or "пользователь"
    return f'<a href="tg://user?id={user.id}">{esc(name)}</a>'


def mod_name(user) -> str:
    """Публичное имя исполнителя; в анонимном режиме без идентификаторов."""
    if flag("ANON_ADMIN"):
        return "модерация"
    uname = f" (@{user.username})" if getattr(user, "username", None) else ""
    return f"{esc(user.full_name)}{uname}"


def mod_decision(user) -> str:
    """Безопасная для публичного чата подпись решения модерации."""
    if flag("ANON_ADMIN"):
        return "Решение модерации."
    return f"Решение: {mod_name(user)}."


def id_mention(uid: int, name: str = "пользователь") -> str:
    """Упоминание по одному id (когда объекта user нет)."""
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def cached_name(uid: int) -> str:
    """Быстрое имя без запросов к API: @ник из кэша либо 'id<num>'. Для клавиатур."""
    for uname, cached in uname_cache.items():
        if cached == uid:
            return f"@{uname}"
    return f"id{uid}"


async def display_name(chat_id: int, uid: int) -> str:
    """Человекочитаемое имя юзера: полное имя/@ник, если удаётся получить, иначе id.

    Сетевые/битые ответы не роняют команду — откатываемся к кэшу и id.
    """
    try:
        m = await bot.get_chat_member(chat_id, uid)
        u = m.user
        uname_cache_add(u)
        if u.full_name and u.full_name.strip():
            return u.full_name.strip()
        if u.username:
            return f"@{u.username}"
    except Exception:
        pass
    return cached_name(uid)


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def message_markdown(message: "Message") -> str:
    """Собрать markdown-текст сообщения, восстановив entity-ссылки.

    Telegram-клиент часть ссылок [текст](url) превращает в rich-entity (и
    markdown из .text пропадает), часть оставляет как есть — из-за этого в
    /rules «одна ссылка вставилась, другие нет». Приводим всё к единому виду
    [текст](url), чтобы render_rules отрисовал все ссылки одинаково.
    """
    text = message.text or message.caption or ""
    ents = message.entities or message.caption_entities or []
    if not text or not ents:
        return text
    # Entity-офсеты Telegram считает в кодовых единицах UTF-16 — режем по ним.
    u16 = text.encode("utf-16-le")

    def sub(off: int, length: int) -> str:
        return u16[off * 2:(off + length) * 2].decode("utf-16-le")

    out, last = [], 0
    for e in sorted(ents, key=lambda x: x.offset):
        if e.offset < last:            # перекрытие (вложенное форматирование) — пропускаем
            continue
        out.append(sub(last, e.offset - last))
        seg = sub(e.offset, e.length)
        if e.type == "text_link" and e.url:
            out.append(f"[{seg}]({e.url})")
        else:                          # обычный url/текст — оставляем как есть (ТГ сам линкует)
            out.append(seg)
        last = e.offset + e.length
    out.append(sub(last, len(u16) // 2 - last))
    return "".join(out)


def _md_after_command(message: "Message") -> str:
    """Markdown-тело команды без ведущего «/cmd» (ссылки восстановлены)."""
    md = message_markdown(message)
    parts = md.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def render_rules(text: str) -> str:
    """Текст со ссылками вида [текст](https://...) -> кликабельный HTML. Безопасно."""
    out, last = [], 0
    for m in _MD_LINK.finditer(text or ""):
        out.append(esc(text[last:m.start()]))
        label, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://", "tg://", "t.me/")):
            if url.startswith("t.me/"):
                url = "https://" + url
            out.append(f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>')
        else:  # не ссылка — оставляем как обычный текст
            out.append(esc(m.group(0)))
        last = m.end()
    out.append(esc(text[last:]))
    return "".join(out)


def name_check(name: str):
    """Проверка имени вступающего с учётом скрытых стоп-слов.

    Возвращает (public_reason, audit_reason, hidden) или (None, None, False),
    если чисто. Для скрытого слова публично пишем нейтральное «недопустимое
    имя», настоящую причину — только в спец-чат (hidden=True), не в журнал.
    """
    if not name:
        return None, None, False
    if textguard.has_profanity(name):
        return "мат в имени", "мат в имени", False
    sw = textguard.find_stopword(name, storage.stopwords(),
                                 fuzzy=flag("FUZZY_STOPWORDS"),
                                 max_distance=num("FUZZY_MAX_DISTANCE"))
    if not sw:
        return None, None, False
    if storage.is_hidden_word(sw):
        return "недопустимое имя", f"скрытое стоп-слово «{sw}» в имени", True
    return f"стоп-слово «{sw}» в имени", f"стоп-слово «{sw}» в имени", False


def flag(name: str) -> bool:
    """Булева настройка с рантайм-оверрайдом из storage (команды /night и т.п.)."""
    return storage.get_flag(name, getattr(config, name))


def num(name: str) -> int:
    """Числовая настройка с рантайм-оверрайдом."""
    return storage.get_num(name, getattr(config, name))


def action_for(name: str) -> str:
    """Действие за фильтр (delete/warn/mute/ban) с рантайм-оверрайдом."""
    return storage.get_str(name, getattr(config, name))


def fmt_when(dt: datetime | None = None) -> str:
    """Время в местном поясе до секунды."""
    dt = dt or now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=config.NIGHT_TZ)))
    return local.strftime("%Y-%m-%d %H:%M:%S")


_DUR_RE = re.compile(r"(\d+)\s*([а-яёa-z]*)", re.I)


def parse_duration(text: str) -> int | None:
    """'3 дня' -> 259200 сек. Нет числа -> None (навсегда)."""
    m = _DUR_RE.search(text.lower())
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2)
    if u.startswith(("нед", "week", "w")):
        k = 604800
    elif u.startswith(("д", "d")):
        k = 86400
    elif u.startswith(("ч", "h")):
        k = 3600
    elif u.startswith(("сек", "s")):
        k = 1
    elif u.startswith(("мин", "м", "min", "m")):
        k = 60
    else:
        k = 3600  # без единицы — считаем часами
    return n * k


def human_duration(seconds: int | None) -> str:
    if not seconds:
        return "навсегда"
    for unit, label in ((604800, "нед"), (86400, "дн"), (3600, "ч"), (60, "мин"), (1, "сек")):
        if seconds % unit == 0 and seconds >= unit:
            return f"{seconds // unit} {label}"
    return f"{seconds} сек"


def audit(actor: str, action: str, target_id: int, target_name: str = "", reason: str = ""):
    """Записать действие модерации в журнал (для /log и панели)."""
    storage.add_audit({
        "ts": fmt_when(), "actor": actor, "action": action,
        "target_id": target_id, "target_name": target_name, "reason": reason,
    })


async def notify_panel(text: str):
    """Разослать уведомление всем, кто авторизован в панели бота."""
    for uid in list(panel_auth):
        try:
            await bot.send_message(uid, text)
        except TelegramBadRequest:
            pass


async def notify_hidden(user, reason: str, text: str = ""):
    """Detail о срабатывании СКРЫТОГО стоп-слова — в ЛС операторам бота.

    Настоящее слово не должно попасть ни в чат группы, ни в /log, ни на глаза
    обычным юзерам. Операторы = владельцы + вошедшие в панель (им бот может
    писать в личку). Плюс необязательный HIDDEN_WORD_CHAT_ID, если задан.
    Другому боту/незапустившему юзеру Telegram писать не даёт — потому и слали
    впустую; теперь адресаты только те, кому доставка реально возможна.
    """
    card = event_card("🕵️ Скрытое стоп-слово", user, text=text, reason=reason)
    targets = set(storage.owners_all()) | set(panel_auth)
    if config.HIDDEN_WORD_CHAT_ID:
        targets.add(config.HIDDEN_WORD_CHAT_ID)
    for uid in targets:
        try:
            await bot.send_message(uid, card)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


async def notify_report(reported: "Message", reporter, card: str):
    """В ЛС админам: пересылка спорного сообщения + карточка с кнопками."""
    chat = reported.chat
    rid = reported.from_user.id
    link = message_link(chat, reported.message_id)
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Перейти к сообщению", url=link)])
    rows += mod_rows(chat.id, rid)
    rows.append([InlineKeyboardButton(text="🗑 Скрыть", callback_data="hide")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    for uid in list(panel_auth):
        try:
            # сам спорный контент (текст/гиф/фото/стикер) — чтобы оценить жалобу
            await bot.forward_message(uid, chat.id, reported.message_id)
        except TelegramBadRequest:
            pass
        try:
            await bot.send_message(uid, card, reply_markup=kb)
        except TelegramBadRequest:
            pass


def message_link(chat, message_id: int) -> str | None:
    """Ссылка на сообщение: t.me/<username>/<id> или t.me/c/<internal>/<id>."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def event_card(title: str, user, *, text: str = "", reason: str = "",
               when: datetime | None = None) -> str:
    """Карточка события для уведомлений: id, имя, юзернейм, текст, время до секунды."""
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    lines = [
        f"<b>{esc(title)}</b>",
        f"ID: <code>{user.id}</code>",
        f"Имя: {mention(user)}",
        f"Юзер: {esc(uname)}",
    ]
    if reason:
        lines.append(f"Причина: {esc(reason)}")
    if text:
        snippet = text if len(text) <= 300 else text[:300] + "…"
        lines.append(f"Сообщение: {esc(snippet)}")
    lines.append(f"Время: {fmt_when(when)}")
    return "\n".join(lines)


# ----------------------------------------------------------------- админы

async def get_admins(chat_id: int) -> set:
    entry = admins_cache.get(chat_id)
    if entry and (now() - entry[1]).total_seconds() < config.ADMIN_CACHE_TTL:
        return entry[0]
    try:
        members = await bot.get_chat_administrators(chat_id)
        ids = {m.user.id for m in members}
        admins_cache[chat_id] = (ids, now())
        return ids
    except TelegramBadRequest:
        return entry[0] if entry else set()


async def is_admin(chat_id: int, user_id: int) -> bool:
    return user_id in await get_admins(chat_id)


def role_perms(role: str) -> set:
    """Набор прав роли: оверрайд из панели (storage) или дефолт из config.ROLES."""
    ov = storage.get_role_perms_override(role)
    if ov is not None:
        return set(ov)
    return set(config.ROLES.get(role, set()))


def set_role_perm(role: str, perm: str, on: bool) -> None:
    """Включить/выключить право у роли (сохраняется в data.json)."""
    cur = role_perms(role)
    cur.add(perm) if on else cur.discard(perm)
    storage.set_role_perms(role, sorted(cur))


def effective_perms(user_id: int) -> set:
    """Итоговые права юзера: личный оверрайд из панели, иначе права его роли."""
    ov = storage.get_user_perms_override(user_id)
    if ov is not None:
        return set(ov)
    role = storage.get_role(user_id)
    return role_perms(role) if role else set()


def has_perm(user_id: int, perm: str) -> bool:
    """Есть ли у юзера внутреннее право (личный оверрайд > права роли)."""
    return perm in effective_perms(user_id)


def role_rank(role: str | None) -> int:
    """Старшинство роли: чем больше — тем старше. Нет роли -> 0."""
    return config.ROLE_RANK.get(role, 0) if role else 0


def role_badge(role: str | None) -> str:
    """Эмодзи-бейдж роли для показа в чате (пусто, если нет)."""
    return config.ROLE_BADGE.get(role, "🎖") if role else ""


def role_title(role: str | None) -> str:
    """Отображаемое имя ранга: кастомное из storage или сам ключ роли."""
    if not role:
        return ""
    return storage.get_role_title(role) or role


def resolve_role(text: str) -> str | None:
    """Ввод (ключ роли ИЛИ кастомное имя, регистронезависимо) -> ключ config.ROLES."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in config.ROLES:
        return t
    for key in config.ROLES:
        if (storage.get_role_title(key) or "").strip().lower() == t:
            return key
    return None


def _roles_hint() -> str:
    """Список ролей для подсказок: «ключ (кастомное имя)», от старшей к младшей."""
    out = []
    for key in sorted(config.ROLES, key=lambda n: role_rank(n), reverse=True):
        t = storage.get_role_title(key)
        out.append(f"{key} ({t})" if t else key)
    return ", ".join(out)


def role_label(role: str | None) -> str:
    """«👑 Админ» — бейдж + (кастомное) имя ранга. Пусто, если роли нет."""
    return f"{role_badge(role)} {esc(role_title(role))}" if role else ""


async def can(chat_id: int, user_id: int, perm: str) -> bool:
    """Право = Telegram-админ (всё) ИЛИ внутренняя роль с этим разрешением."""
    return await is_admin(chat_id, user_id) or has_perm(user_id, perm)


async def is_staff_user(chat_id: int, user_id: int) -> bool:
    """TG-админ, владелец бота или носитель любой внутренней роли."""
    return (storage.is_owner(user_id) or storage.get_role(user_id) is not None
            or await is_admin(chat_id, user_id))


async def _can(message, perm: str) -> bool:
    return bool(message.from_user and await can(message.chat.id, message.from_user.id, perm))


async def _deny_target(chat_id: int, actor_id: int, target_id: int) -> str | None:
    """Причина, по которой actor НЕ вправе наказать target (иначе None).

    Защищает от само-наказания и трогания администрации с учётом ИЕРАРХИИ ролей:
      • себя — никому нельзя (тот самый «сам себя замутил»);
      • самого бота — нельзя;
      • TG-админа чата — нельзя вообще (Telegram и так не даст, но скажем прямо);
      • владельца бота — только другой владелец или TG-админ;
      • носителя внутренней роли — TG-админ/владелец могут всегда; другой персонал —
        только если он СТРОГО старше цели по рангу (старший > модератор и т.п.).
        Равный равного и младший старшего тронуть не может.
    """
    if target_id == actor_id:
        return "🙅 Нельзя применить это к самому себе."
    if bot_self_id and target_id == bot_self_id:
        return "🤖 Это я — меня трогать нельзя."
    if await is_admin(chat_id, target_id):
        return "🛡 Нельзя наказать администратора чата."

    actor_admin = await is_admin(chat_id, actor_id)
    actor_owner = storage.is_owner(actor_id)

    if storage.is_owner(target_id) and not (actor_owner or actor_admin):
        return "🛡 Владельца бота трогать нельзя."

    target_role = storage.get_role(target_id)
    if target_role and not (actor_admin or actor_owner):
        # Наказать персонал может только строго старший по рангу носитель роли.
        if role_rank(storage.get_role(actor_id)) <= role_rank(target_role):
            return (f"🛡 {role_label(target_role)} тебе не по рангу — "
                    "нужен старший по должности или админ чата.")
    return None


async def _deny_and_reply(message, target_id: int) -> bool:
    """Проверка цели для команд-ответов: при отказе отвечает и возвращает True."""
    reason = await _deny_target(message.chat.id, message.from_user.id, target_id)
    if reason:
        await message.answer(reason)
        return True
    return False


async def user_dc(user_id: int) -> int | None:
    """DC (1..5) пользователя по его профильному фото или None (нет фото/скрыто)."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except TelegramBadRequest:
        return None
    if not photos.total_count or not photos.photos or not photos.photos[0]:
        return None
    return dcguard.dc_from_file_id(photos.photos[0][-1].file_id)


async def profile_probe(user_id: int) -> tuple[bool | None, int | None]:
    """Одним запросом достать (есть_ли_аватар, датацентр).

    (None, None) — не смогли узнать (приватность/ошибка). has_photo=False —
    аватара точно нет; dc определяется из file_id аватара, если он есть.
    """
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except TelegramBadRequest:
        return None, None
    if not photos.total_count or not photos.photos or not photos.photos[0]:
        return False, None
    return True, dcguard.dc_from_file_id(photos.photos[0][-1].file_id)


async def risk_evaluate(user) -> tuple[int, list[str], int | None]:
    """Оценить профиль входящего. Возвращает (скор, причины, dc).

    Сигналы (аватар/DC) тянутся из Bot API; чистый скоринг — в riskscore.py.
    """
    has_photo, dc = await profile_probe(user.id)
    dc_flagged = bool(dc is not None and config.DC_BLOCK and dc in config.DC_BLOCK)
    score, reasons = riskscore.score_profile(
        first_name=user.first_name or "",
        last_name=getattr(user, "last_name", "") or "",
        username=user.username,
        user_id=user.id,
        is_premium=bool(getattr(user, "is_premium", False)),
        has_photo=has_photo,
        dc=dc,
        dc_flagged=dc_flagged,
        fresh_id_threshold=num("NEW_ACCOUNT_ID_MIN"),
        weights=getattr(config, "RISK_WEIGHTS", None),
    )
    return score, reasons, dc


def start_probation(chat_id: int, user, score: int, reasons: list[str]) -> None:
    """Взять новичка «под наблюдение» на PROBATION_MINUTES минут."""
    if not flag("PROBATION_ENABLED"):
        return
    mins = max(1, num("PROBATION_MINUTES"))
    probation[(chat_id, user.id)] = {
        "until": now() + timedelta(minutes=mins),
        "score": score,
        "reasons": reasons,
    }


def probation_active(chat_id: int, user_id: int) -> dict | None:
    """Вернуть карточку наблюдения, если оно ещё активно, иначе None (и снять)."""
    st = probation.get((chat_id, user_id))
    if not st:
        return None
    if st["until"] < now():
        probation.pop((chat_id, user_id), None)
        return None
    return st


# ---------------------------------------------------- чистка удалённых аккаунтов

async def kick_deleted(chat_id: int, user_id: int) -> bool:
    """Кикнуть удалёнку (бан + разбан = выкинуть, не блокируя перезаход живого)."""
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        return True
    except TelegramBadRequest as e:
        log.warning("Не смог кикнуть удалёнку %s: %s", user_id, e)
        await _maybe_rights_alert(chat_id, e)
        return False


def known_members(chat_id: int) -> set[int]:
    """Id пользователей, которых бот видел в этом чате (трекинг/капча/новички)."""
    ids: set[int] = set()
    for store in (recent, msgcount, newcomer):
        for (cid, uid) in list(store.keys()):
            if cid == chat_id:
                ids.add(uid)
    return ids


async def sweep_deleted(chat_id: int, *, do_kick: bool = True) -> dict:
    """Bot-API-скан: пройтись по ВИДЕННЫМ участникам чата и кикнуть удалёнок.

    Ограничение: видит лишь тех, кого бот встречал. Полный проход — userbot.py.
    """
    res = {"scanned": 0, "deleted": 0, "kicked": 0}
    for uid in known_members(chat_id):
        try:
            member = await bot.get_chat_member(chat_id, uid)
        except TelegramBadRequest:
            continue
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue
        res["scanned"] += 1
        if deleted.should_kick_member(member):
            res["deleted"] += 1
            if do_kick and await kick_deleted(chat_id, uid):
                res["kicked"] += 1
                stats["deleted_kicked"] += 1
                recent.pop((chat_id, uid), None)
                msgcount.pop((chat_id, uid), None)
    return res


# ---------------------------------------------------------------- картинки

def photo_dir() -> str:
    base = config.PHOTO_DIR
    if not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), base)
    return base


def dhash(img: Image.Image, size: int = 8) -> int:
    img = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = img.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def dhash_from_bytes(data: bytes) -> int | None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return dhash(img)
    except Exception as e:
        log.debug("Не смог распознать изображение: %s", e)
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def load_reference_hashes() -> None:
    ref_hashes.clear()
    base = photo_dir()
    if not os.path.isdir(base):
        log.warning("Папка с эталонами не найдена: %s", base)
        return
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
    for name in sorted(os.listdir(base)):
        if not name.lower().endswith(exts):
            continue
        try:
            with Image.open(os.path.join(base, name)) as img:
                ref_hashes.append((name, dhash(img)))
        except Exception as e:
            log.warning("Эталон %s не загрузился: %s", name, e)
    log.info("Загружено эталонных картинок: %d", len(ref_hashes))


def best_match(h: int) -> tuple[str, int, float] | None:
    if not ref_hashes:
        return None
    name, dist = min(((n, hamming(h, rh)) for n, rh in ref_hashes),
                     key=lambda t: t[1])
    return name, dist, (64 - dist) / 64 * 100


def load_nsfw_detector() -> None:
    """Поднять ViT-классификатор 18+ (nsfwvit). При первом запуске качает модель."""
    if not config.NSFW_ENABLED:
        return
    nsfwvit.load(config.NSFW_MODEL, config.NSFW_THREADS)


async def nsfw_check(data: bytes, tag: str):
    """(метка, вероятность) при 18+ >= порога, иначе None. Совместимо с on_media."""
    if not nsfwvit.available():
        return None
    prob = await asyncio.to_thread(nsfwvit.detect_prob, data)
    if prob is not None and prob >= config.NSFW_THRESHOLD:
        return ("18+", prob)
    return None


async def nsfw_debug(data: bytes, tag: str):
    """Для /check: сырая вероятность 18+ (без применения порога) или None."""
    if not nsfwvit.available():
        return None
    return await asyncio.to_thread(nsfwvit.detect_prob, data)


# ------------------------------------------------- учёт сообщений и зачистка

class TrackMiddleware(BaseMiddleware):
    """Запоминает id+время сообщений от юзеров (для зачистки спама)."""

    async def __call__(self, handler, event, data):
        msg = event
        if (msg.from_user and not msg.from_user.is_bot
                and msg.chat.type in ("group", "supergroup")):
            key = (msg.chat.id, msg.from_user.id)
            known_chats.add(msg.chat.id)
            buf = recent.setdefault(key, deque(maxlen=200))
            buf.append((msg.message_id, now()))
            msgcount[key] = msgcount.get(key, 0) + 1
            storage.bump_activity(msg.chat.id, msg.from_user.id, now().isoformat())
            if msg.from_user.username:                 # запоминаем @ник -> id для таргета по нику
                uname_cache[msg.from_user.username.lower()] = msg.from_user.id
            # Сигнал ЧС: волна сообщений от многих РАЗНЫХ юзеров за короткое окно.
            if flag("AUTO_CRISIS_ENABLED") and not crisis_active(msg.chat.id):
                senders = wave_detect(msg.chat.id, msg.from_user.id)
                if senders >= config.WAVE_USERS:
                    asyncio.create_task(enter_crisis(
                        msg.chat.id, f"волна сообщений ({senders} юзеров за {config.WAVE_WINDOW}с)"))
        return await handler(event, data)


async def purge_recent(chat_id: int, user_id: int) -> int:
    buf = recent.get((chat_id, user_id))
    if not buf:
        return 0
    cutoff = now() - timedelta(seconds=config.PURGE_WINDOW_SECONDS)
    ids = [mid for mid, t in buf if t >= cutoff]
    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
            deleted += len(chunk)
        except TelegramRetryAfter as e:          # флуд-лимит — подождать и повторить
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.delete_messages(chat_id, chunk)
                deleted += len(chunk)
            except TelegramBadRequest:
                pass
        except TelegramBadRequest:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                    deleted += 1
                except TelegramBadRequest:
                    pass
    recent.pop((chat_id, user_id), None)
    return deleted


async def delayed_purge(chat_id: int, user_id: int, delay: float = 3.0):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    n = await purge_recent(chat_id, user_id)
    if n:
        log.info("Догнал и удалил ещё %d сообщений от %s", n, user_id)


async def _safe_dm(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def watchdog():
    """Родитель следит за дочерними: упал -> перезапуск + уведомление владельцу."""
    if IS_CHILD:
        return
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        for c in manager.children():
            bid, owner = c["id"], c.get("owner")
            name = c.get("username") or bid
            if manager.alive(bid):
                child_restarts.pop(bid, None)
                continue
            cnt = child_restarts.get(bid, 0) + 1
            child_restarts[bid] = cnt
            if cnt <= 5:
                try:
                    manager.spawn(c)
                except Exception as e:
                    log.warning("watchdog: не смог поднять %s: %s", bid, e)
                if owner:
                    await _safe_dm(owner, f"⚠️ Бот @{esc(name)} падал — перезапустил (попытка {cnt}).")
            elif cnt == 6 and owner:
                await _safe_dm(owner, f"🛑 Бот @{esc(name)} постоянно падает — больше не "
                                      "перезапускаю. Проверь токен и логи.")


async def janitor():
    while True:
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            return
        n = now()
        for k in [k for k, t in list(flagged.items()) if (n - t).total_seconds() > 60]:
            flagged.pop(k, None)
        cutoff = n - timedelta(seconds=config.PURGE_WINDOW_SECONDS)
        for k in list(recent.keys()):
            buf = recent.get(k)
            while buf and buf[0][1] < cutoff:
                buf.popleft()
            if not buf:
                recent.pop(k, None)
        for k in list(flood.keys()):
            buf = flood.get(k)
            fcut = n - timedelta(seconds=num("ANTIFLOOD_SECONDS"))
            while buf and buf[0] < fcut:
                buf.popleft()
            if not buf:
                flood.pop(k, None)
        for k in list(accept_burst.keys()):
            buf = accept_burst.get(k)
            acut = n - timedelta(seconds=num("ACCEPT_BURST_WINDOW"))
            while buf and buf[0] < acut:
                buf.popleft()
            if not buf:
                accept_burst.pop(k, None)
        for mid in [m for m, v in list(votes.items())
                    if (n - v.get("ts", n)).total_seconds() > 3600]:
            votes.pop(mid, None)
        for k in [k for k, s in list(pending_requests.items()) if not s]:
            pending_requests.pop(k, None)
        ncut = n - timedelta(hours=max(1, num("RESTRICT_NEWCOMERS_HOURS")))
        for k in [k for k, t in list(newcomer.items()) if t < ncut]:
            newcomer.pop(k, None)
        vcut = n - timedelta(hours=1)
        for k in [k for k, v in list(report_votes.items()) if v["ts"] < vcut]:
            report_votes.pop(k, None)
        for k in list(report_times.keys()):
            buf = report_times.get(k)
            while buf and buf[0] < vcut:
                buf.popleft()
            if not buf:
                report_times.pop(k, None)
        for k in [k for k, v in list(probation.items()) if v["until"] < n]:
            probation.pop(k, None)
        # Верификация: истёкшие ожидания (юзер так и не подтвердил номер) —
        # чистим in-memory подсказку; запись в storage.pending_verify оставляем,
        # чтобы юзер мог подтвердить позже (мут снимется только после номера).
        vmins = num("PHONE_VERIFY_MINUTES")
        if vmins > 0:
            vcut2 = n - timedelta(minutes=vmins * 3)  # держим ещё втрое дольше как «хвост»
            for uid in [u for u, st in list(verify_wait.items())
                        if st.get("joined") and st["joined"] < vcut2]:
                verify_wait.pop(uid, None)
        await maybe_autosweep_deleted()
        storage.save_stats(stats)


async def maybe_autosweep_deleted():
    """Периодический автосвип удалёнок по всем виденным чатам (если включён)."""
    global last_deleted_sweep
    if not flag("AUTO_CLEAN_DELETED"):
        return
    every = max(1, num("CLEAN_DELETED_EVERY_HOURS")) * 3600
    if last_deleted_sweep and (now() - last_deleted_sweep).total_seconds() < every:
        return
    last_deleted_sweep = now()
    total = 0
    for chat_id in list(known_chats):
        try:
            res = await sweep_deleted(chat_id, do_kick=True)
        except Exception as e:                    # noqa: BLE001
            log.warning("Автосвип удалёнок в %s упал: %s", chat_id, e)
            continue
        if res["kicked"]:
            total += res["kicked"]
            audit("чистка", f"автосвип: кикнуто удалёнок {res['kicked']}", 0)
    if total:
        await notify_panel(f"🧹 Автосвип: выкинуто удалённых аккаунтов — <b>{total}</b>.")
        log.info("Автосвип удалёнок: кикнуто %d", total)


# ----------------------------------------------------------- наказания

def mod_rows(chat_id: int, uid: int) -> list:
    """Кнопки модерации: бан/мут, мут на срок, бан+чистка, размут."""
    p = f"{chat_id}:{uid}"
    return [
        [InlineKeyboardButton(text="🔨 Бан", callback_data=f"mod:ban:{p}"),
         InlineKeyboardButton(text="🔇 Мут", callback_data=f"mod:mute:{p}")],
        [InlineKeyboardButton(text="Мут 1ч", callback_data=f"mod:mute:{p}:3600"),
         InlineKeyboardButton(text="1д", callback_data=f"mod:mute:{p}:86400"),
         InlineKeyboardButton(text="3д", callback_data=f"mod:mute:{p}:259200")],
        [InlineKeyboardButton(text="🧹 Бан+чистка", callback_data=f"mod:banwipe:{p}"),
         InlineKeyboardButton(text="✅ Размут", callback_data=f"mod:unmute:{p}")],
    ]


def mod_keyboard(chat_id: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=mod_rows(chat_id, uid))


async def report(chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None):
    """Отправить уведомление с учётом тихого режима и лог-чата."""
    target = config.LOG_CHAT_ID
    dest = target if flag("QUIET_MODE") else (target or chat_id)
    if dest is None:
        return
    try:
        await bot.send_message(dest, text, reply_markup=kb)
    except TelegramBadRequest:
        if dest != chat_id:
            try:
                await bot.send_message(chat_id, text, reply_markup=kb)
            except TelegramBadRequest:
                pass


async def _autodelete(chat_id: int, message_id: int, seconds: float):
    try:
        await asyncio.sleep(seconds)
        await bot.delete_message(chat_id, message_id)
    except (asyncio.CancelledError, TelegramBadRequest):
        pass


async def _maybe_rights_alert(chat_id: int, err: Exception):
    """Если ошибка похожа на потерю прав админа — разово предупредить владельцев."""
    s = str(err).lower()
    if not ("not enough rights" in s or "chat_admin_required" in s
            or "need administrator" in s or "can't remove chat owner" in s):
        return
    last = rights_alert.get(chat_id)
    if not last or (now() - last).total_seconds() > 3600:
        rights_alert[chat_id] = now()
        await notify_panel(f"⚠️ Бот, похоже, потерял права админа в чате "
                           f"<code>{chat_id}</code> — модерация не работает. Верни админку.")


async def ban_user(chat_id: int, user_id: int, seconds: int | None = None):
    until = (now() + timedelta(seconds=seconds)) if seconds else None
    try:
        await bot.ban_chat_member(chat_id, user_id, until_date=until)
        stats["banned"] += 1
        log.info("Забанен %s в чате %s на %s", user_id, chat_id, human_duration(seconds))
    except TelegramBadRequest as e:
        log.warning("Не смог забанить %s (админ? бот не админ?): %s", user_id, e)
        await _maybe_rights_alert(chat_id, e)


async def mute_user(chat_id: int, user_id: int, seconds: int | None = None):
    until = (now() + timedelta(seconds=seconds)) if seconds else None
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE, until_date=until)
    except TelegramBadRequest as e:
        log.warning("Не смог замутить %s: %s", user_id, e)
        await _maybe_rights_alert(chat_id, e)


async def apply_punishment(message: Message, reason: str, action: str,
                           audit_reason: str | None = None, hidden: bool = False):
    """Удалить сообщение и применить действие: delete | warn | mute | ban.

    reason — что показываем публично в чате; audit_reason (если задан) — что
    пишем в журнал/панель. hidden=True (скрытое стоп-слово): в журнал и панель
    настоящее слово НЕ пишем (только generic reason), а detail шлём в спец-чат.
    """
    chat_id = message.chat.id
    user = message.from_user
    uid = user.id
    msg_text = message.text or message.caption or ""
    audit_reason = audit_reason or reason
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    limit = num("WARN_LIMIT")
    if action == "warn":
        n = storage.add_warn(chat_id, uid)
        if n >= limit:
            storage.reset_warns(chat_id, uid)
            if action_for("WARN_ACTION") == "ban":
                await ban_user(chat_id, uid)
                await report(chat_id, f"🔨 {mention(user)} забанен: лимит предупреждений ({esc(reason)}).")
            else:
                await mute_user(chat_id, uid)
                await report(chat_id, f"🔇 {mention(user)} в муте: лимит предупреждений ({esc(reason)}).",
                             mod_keyboard(chat_id, uid))
        else:
            await report(chat_id, f"⚠️ {mention(user)}: предупреждение {n}/{limit} — {esc(reason)}.")
    elif action == "mute":
        await mute_user(chat_id, uid)
        await report(chat_id, f"🔇 {mention(user)} в муте: {esc(reason)}.", mod_keyboard(chat_id, uid))
    elif action == "ban":
        await ban_user(chat_id, uid)
        await report(chat_id, f"🔨 {mention(user)} забанен: {esc(reason)}.")
    # action == "delete": тихо удаляем, без уведомления в чат

    if hidden:
        # В журнал — обезличенно (без настоящего слова). Detail — только в спец-чат.
        audit("авто-фильтр", action, uid, user.full_name, reason)
        await notify_hidden(user, audit_reason, msg_text)
    else:
        audit("авто-фильтр", action, uid, user.full_name, audit_reason)
        if flag("NOTIFY_VIOLATIONS"):
            await notify_panel(event_card("🚨 Нарушение", user, text=msg_text, reason=audit_reason))


# --------------------------------------------------- проверки сообщений

def is_night() -> bool:
    if not flag("NIGHT_MODE"):
        return False
    h = datetime.now(tz=timezone(timedelta(hours=config.NIGHT_TZ))).hour
    s, e = config.NIGHT_START, config.NIGHT_END
    return (s <= h or h < e) if s > e else (s <= h < e)


def is_service(msg: Message) -> bool:
    return bool(msg.new_chat_members or msg.left_chat_member or msg.new_chat_title
                or msg.new_chat_photo or msg.delete_chat_photo or msg.pinned_message
                or msg.group_chat_created or msg.video_chat_started
                or msg.video_chat_ended or msg.message_auto_delete_timer_changed)


def has_link(msg: Message) -> bool:
    txt = msg.text or msg.caption or ""
    if LINK_RE.search(txt):
        return True
    for e in (msg.entities or []) + (msg.caption_entities or []):
        if e.type in ("url", "text_link"):
            return True
        if e.type in ("mention", "text_mention") and not flag("ALLOW_MENTIONS"):
            return True
    return False


def has_apk(msg: Message) -> bool:
    d = msg.document
    return bool(d and ((d.file_name or "").lower().endswith(".apk")
                       or d.mime_type == "application/vnd.android.package-archive"))


def has_premium_emoji(msg: Message) -> bool:
    return any(e.type == "custom_emoji"
               for e in (msg.entities or []) + (msg.caption_entities or []))


def has_media(msg: Message) -> bool:
    return bool(msg.photo or msg.video or msg.animation or msg.sticker
                or msg.document or msg.audio or msg.voice or msg.video_note)


def antiflood_hit(chat_id: int, user_id: int) -> bool:
    secs = num("ANTIFLOOD_SECONDS")
    buf = flood.setdefault((chat_id, user_id), deque(maxlen=50))
    t = now()
    buf.append(t)
    cut = t - timedelta(seconds=secs)
    while buf and buf[0] < cut:
        buf.popleft()
    if len(buf) > num("ANTIFLOOD_COUNT"):
        buf.clear()
        return True
    return False


# Команды, доступные ВСЕМ участникам (не считаются «чужой админ-командой»).
PUBLIC_CMDS = {"rules", "report", "ping", "help", "vb", "start", "privacy",
               "kubik", "dice", "game"}


def _cmd_name(text: str) -> str | None:
    """Имя команды из текста ('/Ban@bot x' -> 'ban'); None, если это не команда."""
    m = re.match(r"^/([A-Za-z0-9_]+)", text or "")
    return m.group(1).split("@")[0].lower() if m else None


class ModerationMiddleware(BaseMiddleware):
    """Фильтрует сообщения не-админов; нарушение -> наказание, сообщение не идёт дальше."""

    async def __call__(self, handler, event, data):
        msg = event
        if (msg.chat.type in ("group", "supergroup") and not is_service(msg)
                and await self._moderate(msg)):
            return  # съели сообщение
        return await handler(event, data)

    async def _moderate(self, msg: Message) -> bool:
        chat_id = msg.chat.id

        # Идёт фото-капча с вводом кода: любое сообщение новичка перехватываем
        # (удаляем; на фото-шаге сверяем код). Раньше всех прочих проверок.
        if msg.from_user:
            st = pending.get((chat_id, msg.from_user.id))
            if st and st.get("steps"):
                await handle_captcha_message(msg, st)
                return True

        # Сообщения «от имени канала» — у них from_user может быть None.
        sc = msg.sender_chat
        if sc and flag("BLOCK_CHANNEL_MESSAGES") and sc.id != chat_id and not msg.is_automatic_forward:
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            try:
                await bot.ban_chat_sender_chat(chat_id, sc.id)
            except TelegramBadRequest as e:
                log.warning("Не смог забанить канал %s: %s", sc.id, e)
            await report(chat_id, f"🚫 Заблокирован постинг от имени канала «{esc(sc.title or sc.id)}».")
            return True

        user = msg.from_user
        if (not user or user.is_bot or await is_admin(chat_id, user.id)
                or storage.is_trusted(chat_id, user.id)):
            return False

        regular = is_regular(chat_id, user.id)

        # Наблюдение (probation): подозрительный новичок в первые минуты.
        # Классический кейс — «иностранный акк спустя пару минут кидает фотки/ссылку».
        if flag("PROBATION_ENABLED"):
            watch = probation_active(chat_id, user.id)
            if watch:
                bad = None
                if flag("PROBATION_ON_MEDIA") and has_media(msg):
                    bad = "медиа от наблюдаемого новичка"
                elif flag("PROBATION_ON_LINK") and has_link(msg):
                    bad = "ссылка от наблюдаемого новичка"
                if bad:
                    probation.pop((chat_id, user.id), None)
                    reason = f"{bad} (риск {watch['score']})"
                    await apply_punishment(msg, reason, action_for("PROBATION_ACTION"))
                    stats["risk_muted"] += 1
                    if flag("NOTIFY_VIOLATIONS"):
                        await notify_panel(event_card("🚨 Сработало наблюдение", user,
                                                      reason=reason))
                    return True


        # Публичные команды (/rules /report /ping /vb /help) и команды персонала
        # (носителей ролей) пропускаем как обычно.
        cmd = _cmd_name(msg.text or "")
        if (cmd and cmd not in PUBLIC_CMDS
                and not storage.get_role(user.id) and not storage.is_owner(user.id)):
            if flag("DELETE_USER_COMMANDS"):
                try:
                    await msg.delete()
                except TelegramBadRequest:
                    pass
                return True  # съели — хендлер не запустится

        # Ночной режим.
        if is_night():
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            last = night_notice.get(chat_id)
            if not last or (now() - last).total_seconds() > 600:
                night_notice[chat_id] = now()
                await report(chat_id, "🌙 Ночной режим: сейчас писать могут только админы.")
            return True

        # Ограничение новичков: первые N часов нельзя ссылки/медиа.
        hrs = num("RESTRICT_NEWCOMERS_HOURS")
        if hrs > 0:
            joined = newcomer.get((chat_id, user.id))
            if joined and (now() - joined).total_seconds() < hrs * 3600:
                if has_link(msg) or has_media(msg):
                    await apply_punishment(msg, f"новичок (первые {hrs}ч): ссылки/медиа запрещены", "delete")
                    return True

        # Пересылки.
        if flag("BLOCK_FORWARDS") and (msg.forward_origin is not None or msg.forward_date is not None):
            await apply_punishment(msg, "пересылка сообщений", action_for("FORWARD_ACTION"))
            return True

        # Файлы .apk.
        if flag("BLOCK_APK") and has_apk(msg):
            await apply_punishment(msg, "файл .apk", "delete")
            return True

        # Премиум/кастом-эмодзи.
        if flag("BLOCK_PREMIUM_EMOJI") and has_premium_emoji(msg):
            await apply_punishment(msg, "премиум-эмодзи", "delete")
            return True

        # Ссылки (если не в белом списке).
        if (flag("BLOCK_LINKS") and not regular
                and not storage.link_allowed(chat_id, user.id)
                and has_link(msg)):
            await apply_punishment(msg, "ссылка/инвайт", action_for("LINK_ACTION"))
            return True

        # Антифлуд.
        if flag("ANTIFLOOD_ENABLED") and not regular and antiflood_hit(chat_id, user.id):
            await apply_punishment(msg, "флуд", action_for("ANTIFLOOD_ACTION"))
            return True

        # Анти-повтор: одинаковые сообщения подряд.
        body = (msg.text or msg.caption or "").strip().lower()
        if flag("ANTIREPEAT_ENABLED") and not regular and body:
            rk = (chat_id, user.id)
            st = repeat.get(rk)
            if st and st[0] == body:
                st[1] += 1
            else:
                repeat[rk] = [body, 1]
                st = repeat[rk]
            if st[1] >= num("ANTIREPEAT_COUNT"):
                repeat.pop(rk, None)
                await apply_punishment(msg, "повтор сообщений", action_for("ANTIREPEAT_ACTION"))
                return True

        # Мат и стоп-слова.
        text = msg.text or msg.caption or ""
        if text and not regular:
            if flag("ANTIMAT_ENABLED") and textguard.has_profanity(text):
                await apply_punishment(msg, "мат", action_for("TEXT_ACTION"))
                return True
            sw = textguard.find_stopword(text, storage.stopwords(),
                                         fuzzy=flag("FUZZY_STOPWORDS"),
                                         max_distance=num("FUZZY_MAX_DISTANCE"))
            if sw:
                if storage.is_hidden_word(sw):
                    # Скрытое слово: в чате — обезличенная причина; настоящее слово
                    # не в журнал, а только в спец-чат (hidden=True).
                    await apply_punishment(msg, "нарушение правил", action_for("TEXT_ACTION"),
                                           audit_reason=f"скрытое стоп-слово «{sw}»", hidden=True)
                else:
                    await apply_punishment(msg, f"стоп-слово «{sw}»", action_for("TEXT_ACTION"))
                return True

        # Анти-деанон/угрозы по ТЕКСТУ: слив чужих ПДн, угрозы, деанон-ресурсы
        # (@...dnn и т.п.) — травля/деанон админов. Проверяем у ВСЕХ не-админов
        # (даже старожилов): деанон опаснее ложняка, мут обратим — админы онлайн
        # и снимут ложную тревогу кнопкой.
        if text and flag("TEXT_DEANON_ENABLED"):
            hit, why = deanon.scan_text(text, num("DEANON_MIN_HITS"))
            if hit:
                stats["deanon_text"] = stats.get("deanon_text", 0) + 1
                await apply_punishment(msg, "деанон/угроза (себя задеанонь)",
                                       action_for("TEXT_DEANON_ACTION"),
                                       audit_reason=f"деанон-текст: {why}")
                return True
        return False


# ---------------------------------------------------------------- капча

_FONT_PATHS = [
    "arial.ttf", "Arial.ttf",                                  # Windows (по имени)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",    # Debian/Ubuntu
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _captcha_font(size: int):
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)   # Pillow >= 10: масштабируемый дефолт


def make_captcha_image(code: str) -> bytes:
    """Картинка с цифрами кода (со сдвигом/наклоном/шумом) — антибот-капча. PNG в память."""
    pad, gap = 18, 40
    W, H = pad * 2 + gap * len(code), 90
    img = Image.new("RGB", (W, H), (238, 240, 245))
    draw = ImageDraw.Draw(img)
    # фоновый шум: линии и точки
    for _ in range(6):
        draw.line([(random.randint(0, W), random.randint(0, H)),
                   (random.randint(0, W), random.randint(0, H))],
                  fill=(random.randint(150, 205),) * 3, width=1)
    for _ in range(220):
        draw.point((random.randint(0, W), random.randint(0, H)),
                   fill=(random.randint(150, 210),) * 3)
    # цифры по одной, каждая — со своим наклоном/цветом/сдвигом
    for i, ch in enumerate(code):
        size = random.randint(40, 52)
        glyph = Image.new("RGBA", (size + 14, size + 20), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glyph)
        color = (random.randint(0, 90), random.randint(0, 90), random.randint(0, 110))
        gd.text((6, 4), ch, font=_captcha_font(size), fill=color + (255,))
        glyph = glyph.rotate(random.randint(-28, 28), expand=True, resample=Image.BICUBIC)
        x = pad + i * gap - 8
        y = (H - glyph.height) // 2 + random.randint(-6, 6)
        img.paste(glyph, (x, y), glyph)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def captcha_steps_count() -> int:
    """Сколько заданий в капче — зажато в диапазон 1..3."""
    return max(1, min(3, num("CAPTCHA_STEPS")))


def build_questions() -> list[dict]:
    total = captcha_steps_count()
    q, ans, wrongs = random.choice(COMMONSENSE)
    name, n = random.choice(list(SHAPES.items()))
    if flag("CAPTCHA_IMAGE"):
        code = "".join(random.choice("0123456789") for _ in range(num("CAPTCHA_DIGITS")))
        step1 = {"q": f"Шаг 1/{total}. Введите цифры с картинки одним сообщением:",
                 "answer": code, "kind": "image"}
    else:
        a, b = random.randint(2, 9), random.randint(2, 9)
        step1 = {"q": f"Шаг 1/{total}. Реши пример:\n<b>{a} + {b} = ?</b>",
                 "answer": str(a + b), "kind": "num"}
    steps = [
        step1,
        {"q": f"Шаг 2/{total}. {esc(q)}", "answer": ans, "wrongs": wrongs},
        {"q": f"Шаг 3/{total}. Сколько углов у <b>{name}</b>? (ответ цифрой)",
         "answer": str(n), "kind": "num"},
    ]
    return steps[:total]


def options_for(step: dict) -> list[str]:
    if step.get("kind") == "num":
        n = int(step["answer"])
        opts = {n}
        while len(opts) < 4:
            cand = n + random.randint(-3, 3)
            if cand >= 1:
                opts.add(cand)
        result = [str(x) for x in opts]
    else:
        wrongs = list(step["wrongs"])
        random.shuffle(wrongs)
        result = [step["answer"]] + wrongs[:3]
    random.shuffle(result)
    return result


def captcha_markup(idx: int, options: list[str]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=o, callback_data=f"cap:{idx}:{o}") for o in options]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def captcha_photo_kb() -> InlineKeyboardMarkup:
    """Кнопка под фото-капчей: сменить картинку, если цифры неразборчивы."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Заменить картинку", callback_data="capnew")]])


def captcha_caption(user, question: str) -> str:
    total = captcha_steps_count()
    task_word = {1: "задания", 2: "заданий", 3: "заданий"}.get(total, "заданий")
    return (f"👋 {mention(user)}, добро пожаловать!\n"
            f"Пройди проверку из <b>{total} {task_word}</b> за <b>{num('CAPTCHA_TIMEOUT')} сек</b>. "
            f"Ошибка или тишина — бан.\n\n{question}")


async def cleanup(chat_id: int, user_id: int, *, delete_msg: bool = True):
    state = pending.pop((chat_id, user_id), None)
    if not state:
        return
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    if delete_msg and config.DELETE_CAPTCHA_MESSAGE and state.get("msg_id"):
        try:
            await bot.delete_message(chat_id, state["msg_id"])
        except TelegramBadRequest:
            pass


async def captcha_timeout(chat_id: int, user_id: int):
    try:
        await asyncio.sleep(num("CAPTCHA_TIMEOUT"))
    except asyncio.CancelledError:
        return
    if (chat_id, user_id) in pending:
        stats["failed"] += 1
        await ban_user(chat_id, user_id)
        audit("капча", "ban (таймаут)", user_id)
        await cleanup(chat_id, user_id)


async def send_welcome(chat_id: int, user):
    if not flag("WELCOME_ENABLED"):
        return
    kb = None
    if config.WELCOME_BUTTONS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=b[0], url=b[1])] for b in config.WELCOME_BUTTONS
        ])
    text = storage.get_str("WELCOME_TEXT", config.WELCOME_TEXT)
    try:
        await bot.send_message(chat_id, f"{mention(user)}, {render_rules(text)}",
                               reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass


# ------------------------- выдача доступа после капчи -------------------------

async def grant_full_or_quarantine(chat_id: int, user_id: int) -> None:
    """Снять капча-ограничения: либо медиа-карантин новичка на сутки
    (только текст, media запрещены — нативно, until_date), либо полный доступ."""
    hrs = num("NEWCOMER_MEDIA_HOURS")
    if hrs > 0:
        until = now() + timedelta(hours=hrs)
        try:
            await bot.restrict_chat_member(
                chat_id, user_id, permissions=NEWCOMER_QUARANTINE, until_date=until,
                use_independent_chat_permissions=True)
            return
        except TelegramBadRequest as e:
            log.warning("Не смог включить медиа-карантин %s: %s", user_id, e)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=FULL)
    except TelegramBadRequest as e:
        log.warning("Не смог снять ограничения с %s: %s", user_id, e)


async def grant_member_access(chat_id: int, user) -> None:
    """Финально впустить участника (капча + верификация пройдены)."""
    await grant_full_or_quarantine(chat_id, user.id)
    await send_welcome(chat_id, user)


async def verify_timeout(user_id: int):
    """Таймер ожидания номера: подсказку в группе убираем, юзер остаётся в муте."""
    mins = num("PHONE_VERIFY_MINUTES")
    if mins <= 0:
        return
    try:
        await asyncio.sleep(mins * 60)
    except asyncio.CancelledError:
        return
    st = verify_wait.get(user_id)
    if not st or storage.is_phone_verified(user_id):
        return
    notice = st.get("notice_id")
    if notice:
        try:
            await bot.delete_message(st["chat_id"], notice)
        except TelegramBadRequest:
            pass
    # оставляем запись в storage.pending_verify — юзер ещё сможет подтвердить позже


async def send_phone_prompt(chat_id: int, user) -> None:
    """В группе: остался шаг — верификация по номеру в ЛС бота (кнопка-ссылка)."""
    notice_id = None
    kb = None
    if bot_username:
        url = f"https://t.me/{bot_username}?start=verify_{chat_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить, что я человек", url=url)]])
    try:
        sent = await bot.send_message(
            chat_id,
            f"{mention(user)}, остался последний шаг ✅\n"
            "Нажми кнопку ниже и <b>поделись своим номером</b> в личке бота — "
            "так мы убеждаемся, что ты живой человек, а не бот. "
            "До подтверждения писать в чат нельзя.",
            reply_markup=kb, disable_web_page_preview=True)
        notice_id = sent.message_id
    except TelegramBadRequest:
        pass
    st = verify_wait.get(user.id) or {}
    old = st.get("task")
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(verify_timeout(user.id))
    verify_wait[user.id] = {"chat_id": chat_id, "notice_id": notice_id,
                            "name": user.full_name or "", "joined": now(), "task": task}
    storage.set_pending_verify(user.id, chat_id, notice_id, fmt_when())


async def finish_captcha(chat_id: int, user) -> None:
    """Капча пройдена. Дальше — верификация по номеру (если включена и ещё не
    пройдена глобально), иначе сразу выдаём доступ (с медиа-карантином)."""
    stats["passed"] += 1
    if flag("PHONE_VERIFY_ENABLED") and not storage.is_phone_verified(user.id):
        try:                                   # держим в муте до подтверждения номера
            await bot.restrict_chat_member(chat_id, user.id, permissions=MUTE)
        except TelegramBadRequest:
            pass
        await send_phone_prompt(chat_id, user)
        log.info("Юзер %s прошёл капчу — ждём верификацию по номеру.", user.id)
        return
    await grant_member_access(chat_id, user)
    log.info("Юзер %s прошёл капчу — доступ выдан.", user.id)


async def check_raid(chat_id: int) -> bool:
    """Зарегистрировать вход и вернуть True, если идёт рейд/локдаун."""
    active = raid_until.get(chat_id)
    if active and active > now():
        return True
    if not flag("ANTIRAID_ENABLED"):
        return False
    buf = raid_joins.setdefault(chat_id, deque(maxlen=200))
    t = now()
    buf.append(t)
    cut = t - timedelta(seconds=config.RAID_WINDOW)
    while buf and buf[0] < cut:
        buf.popleft()
    if len(buf) >= config.RAID_JOINS:
        raid_until[chat_id] = t + timedelta(seconds=config.RAID_LOCKDOWN)
        stats["raids"] += 1
        await report(chat_id, f"🛡 Похоже на рейд: {len(buf)} входов за {config.RAID_WINDOW}с. "
                              f"Локдаун на {config.RAID_LOCKDOWN // 60} мин.")
        await notify_panel(f"🛡 РЕЙД в чате <code>{chat_id}</code>: "
                           f"{len(buf)} входов за {config.RAID_WINDOW}с.")
        await enter_crisis(chat_id, f"всплеск входов ({len(buf)} за {config.RAID_WINDOW}с)")
        return True
    return False


# ======================= Автономный режим ЧС (автопилот) ====================
# Ловим рейд по 3 сигналам (входы/заявки/волна сообщений), МЯГКО ужесточаем
# защиту, пингуем модеров; не откликнулись — эскалируем. Стихло — откатываем.

# Настройки, которые ЧС-режим временно перекрывает (уровень 1 «мягкое усиление»).
# Сохраняем прежние значения в crisis[...]['saved'] и возвращаем при выходе.
_CRISIS_FLAGS_ON = ("RISK_ENABLED", "PROBATION_ENABLED", "PROBATION_ON_MEDIA",
                    "PROBATION_ON_LINK", "ANTIFLOOD_ENABLED", "CHECK_JOIN_NAMES")
_CRISIS_FLAGS_OFF = ("AUTO_ACCEPT",)
# Числа: строже пороги на время ЧС (капча в 3 шага, ниже риск-пороги, короче флуд-окно).
_CRISIS_NUMS = {"CAPTCHA_STEPS": 3, "RISK_WATCH_THRESHOLD": 30,
                "ANTIFLOOD_COUNT": 4, "PROBATION_MINUTES": 20}


def wave_detect(chat_id: int, uid: int, t: datetime | None = None) -> int:
    """Регистрирует сообщение и возвращает число РАЗНЫХ отправителей в окне WAVE_WINDOW."""
    t = t or now()
    buf = msg_wave.setdefault(chat_id, deque(maxlen=400))
    buf.append((t, uid))
    cut = t - timedelta(seconds=config.WAVE_WINDOW)
    while buf and buf[0][0] < cut:
        buf.popleft()
    return len({u for _, u in buf})


def _parse_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_regular(chat_id: int, user_id: int) -> bool:
    if not flag("REGULARS_ENABLED"):
        return False
    act = storage.get_activity(chat_id, user_id)
    if not act:
        return False
    first = _parse_ts(act.get("first_seen"))
    if not first:
        return False
    return ((now() - first).days >= num("REGULAR_MIN_DAYS")
            and act.get("msgs", 0) >= num("REGULAR_MIN_MSGS"))


def crisis_active(chat_id: int) -> bool:
    st = crisis.get(chat_id)
    return bool(st and st.get("until", now()) > now())


def _join_notifications_allowed(chat_id: int, raid: bool = False) -> bool:
    """Не отправлять уведомления о входах во время рейда или ЧС."""
    return flag("NOTIFY_JOINS") and not raid and not crisis_active(chat_id)


async def _crisis_staff_ping(chat_id: int, reason: str) -> None:
    """@-пинг носителей ролей в чате + карточка в ЛС с кнопкой «Беру контроль»."""
    mentions = []
    for uid, role in storage.roles_all().items():
        try:
            m = await bot.get_chat_member(chat_id, int(uid))
        except (TelegramBadRequest, ValueError, TypeError):
            continue
        if m.status in ("left", "kicked"):
            continue
        u = getattr(m, "user", None)
        if u:
            mentions.append(mention(u))
    line = "модераторы" if flag("ANON_ADMIN") else (" ".join(mentions[:10]) or "модераторы")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛡 Беру контроль", callback_data=f"crisisack:{chat_id}")]])
    await report(chat_id, f"🚨 <b>Похоже на рейд</b> — включил усиленную защиту ({esc(reason)}).\n"
                          f"{line}, если вы на связи — нажмите кнопку, иначе через "
                          f"{config.CRISIS_ESCALATE_AFTER // 60} мин ужесточу автоматически.", kb)
    await notify_panel(f"🚨 ЧС в чате <code>{chat_id}</code>: {esc(reason)}. Автозащита включена.")


async def enter_crisis(chat_id: int, reason: str, force: bool = False) -> None:
    """Войти в ЧС-режим: сохранить настройки, мягко ужесточить, пингануть модеров."""
    if not force and not flag("AUTO_CRISIS_ENABLED"):
        return
    st = crisis.get(chat_id)
    t = now()
    if st and st.get("until", t) > t:
        # Уже в ЧС — просто продлеваем окно затишья.
        st["until"] = t + timedelta(seconds=config.CRISIS_COOLDOWN)
        st["last_signal"] = t
        return
    # Снимок текущих значений — вернём при выходе.
    saved = {"flags": {}, "nums": {}}
    for name in _CRISIS_FLAGS_ON + _CRISIS_FLAGS_OFF:
        saved["flags"][name] = flag(name)
    for name in _CRISIS_NUMS:
        saved["nums"][name] = num(name)
    crisis[chat_id] = {
        "level": 1, "reason": reason, "entered": t, "acked": False,
        "last_signal": t, "until": t + timedelta(seconds=config.CRISIS_COOLDOWN),
        "saved": saved,
    }
    stats["crises"] += 1
    for name in _CRISIS_FLAGS_ON:
        storage.set_flag(name, True)
    for name in _CRISIS_FLAGS_OFF:
        storage.set_flag(name, False)
    for name, val in _CRISIS_NUMS.items():
        storage.set_num(name, val)
    audit("ЧС-автопилот", "включён режим ЧС", 0, "", reason)
    await _crisis_staff_ping(chat_id, reason)


async def escalate_crisis(chat_id: int) -> None:
    """Уровень 2: модеры не откликнулись — автобан входящих на время ЧС."""
    st = crisis.get(chat_id)
    if not st or st["level"] >= 2:
        return
    st["level"] = 2
    # Запоминаем прежнее значение LOCKDOWN ДО включения, чтобы вернуть при выходе.
    st["saved"].setdefault("flags", {}).setdefault("LOCKDOWN", flag("LOCKDOWN"))
    storage.set_flag("LOCKDOWN", True)
    audit("ЧС-автопилот", "эскалация: автобан входящих", 0, "", "модеры не откликнулись")
    await report(chat_id, "🛡 Модераторы не на связи — <b>ужесточаю</b>: новые входящие "
                          "временно банятся до конца ЧС.")
    await notify_panel(f"🛡 ЧС в <code>{chat_id}</code> эскалирована (автобан входящих).")


async def exit_crisis(chat_id: int, manual: bool = False) -> None:
    """Выйти из ЧС: вернуть сохранённые настройки как было."""
    st = crisis.pop(chat_id, None)
    if not st:
        return
    saved = st.get("saved", {})
    for name, val in saved.get("flags", {}).items():
        storage.set_flag(name, val)
    for name, val in saved.get("nums", {}).items():
        storage.set_num(name, val)
    raid_until.pop(chat_id, None)
    msg_wave.pop(chat_id, None)
    audit("ЧС-автопилот", "режим ЧС снят" + (" (вручную)" if manual else ""), 0)
    await report(chat_id, "✅ Похоже, рейд закончился — вернул обычный режим модерации.")
    await notify_panel(f"✅ ЧС в чате <code>{chat_id}</code> завершён, настройки восстановлены.")


async def crisis_monitor():
    """Фон: эскалация при молчании модеров и авто-выход, когда рейд стих."""
    while True:
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return
        t = now()
        for chat_id in list(crisis.keys()):
            st = crisis.get(chat_id)
            if not st:
                continue
            # Предохранитель: слишком долго в ЧС -> выходим.
            if (t - st["entered"]).total_seconds() > config.CRISIS_MAX_MINUTES * 60:
                await exit_crisis(chat_id)
                continue
            # Затишье дольше COOLDOWN -> рейд закончился.
            if st.get("until", t) <= t:
                await exit_crisis(chat_id)
                continue
            # Модеры молчат дольше порога -> эскалация.
            if (not st["acked"] and st["level"] < 2
                    and (t - st["entered"]).total_seconds() > config.CRISIS_ESCALATE_AFTER):
                await escalate_crisis(chat_id)


async def challenge(chat_id: int, user) -> None:
    if user.is_bot:
        return
    key = (chat_id, user.id)
    if key in pending:
        return
    pending[key] = {"steps": None, "idx": 0, "msg_id": None, "task": None}

    try:
        if await is_admin(chat_id, user.id) or storage.is_trusted(chat_id, user.id):
            pending.pop(key, None)
            return

        newcomer[key] = now()

        # Сначала фиксируем вход в антирейде, чтобы не отправить лишнее
        # уведомление о входе, который сам включил локдаун.
        raid = flag("LOCKDOWN")
        if not raid:
            raid = await check_raid(chat_id)
        if _join_notifications_allowed(chat_id, raid):
            await notify_panel(event_card("👤 Вход в группу", user))

        # ПАНИКА (/lockdown): банить всех входящих без капчи.
        if flag("LOCKDOWN"):
            pending.pop(key, None)
            await ban_user(chat_id, user.id)
            audit("локдаун", "бан входа", user.id, user.full_name)
            return

        # Антирейд: при рейде либо баним входящих, либо просто продолжаем капчу.
        if raid and config.RAID_AUTOBAN:
            pending.pop(key, None)
            await ban_user(chat_id, user.id)
            if _join_notifications_allowed(chat_id, raid):
                await notify_panel(event_card("🛡 Бан по антирейду", user))
            return

        # Фильтр по датацентру (DC5 = частый у ботов).
        if config.DC_BLOCK and flag("DC_CHECK_JOIN"):
            dc = await user_dc(user.id)
            if dc in config.DC_BLOCK:
                pending.pop(key, None)
                await ban_user(chat_id, user.id)
                audit("DC-фильтр", f"бан DC{dc}", user.id, user.full_name)
                return

        # Стоп-слова/мат в имени вступающего.
        if flag("CHECK_JOIN_NAMES"):
            public, why, hidden = name_check(f"{user.full_name or ''} {user.username or ''}")
            if public:
                pending.pop(key, None)
                await ban_user(chat_id, user.id)
                if hidden:
                    audit("имена", "недопустимое имя", user.id, user.full_name)
                    await notify_hidden(user, why)
                else:
                    audit("имена", why, user.id, user.full_name)
                await report(chat_id, f"🚫 {mention(user)} забанен на входе: {esc(public)}.")
                return

        # Риск-скоринг профиля: иностранное имя, случайный ник, нет фото,
        # свежий аккаунт и т.п. Высокий скор -> жёсткое действие сразу;
        # средний -> берём «под наблюдение» (probation) на первые минуты.
        if flag("RISK_ENABLED"):
            score, reasons, _dc = await risk_evaluate(user)
            v = riskscore.verdict(score, num("RISK_WATCH_THRESHOLD"),
                                  num("RISK_BAN_THRESHOLD"))
            if v == "hard":
                act = action_for("RISK_ACTION")
                why = "; ".join(reasons[:4])
                if act == "ban":
                    pending.pop(key, None)
                    await ban_user(chat_id, user.id)
                    stats["risk_muted"] += 1
                    audit("риск-фильтр", f"бан входа (скор {score})", user.id, user.full_name)
                    await report(chat_id, f"🚫 {mention(user)} забанен на входе — "
                                          f"риск {score}: {esc(why)}.")
                    return
                if act == "mute":
                    pending.pop(key, None)
                    await mute_user(chat_id, user.id)
                    stats["risk_muted"] += 1
                    audit("риск-фильтр", f"мут входа (скор {score})", user.id, user.full_name)
                    await report(chat_id, f"🔇 {mention(user)} в муте на входе — "
                                          f"риск {score}: {esc(why)}.", mod_keyboard(chat_id, user.id))
                    if flag("NOTIFY_VIOLATIONS") and not raid and not crisis_active(chat_id):
                        await notify_panel(event_card("🚨 Риск-профиль (мут на входе)",
                                                      user, reason=f"скор {score}: {why}"))
                    return
                # act == "captcha" — на капчу, но с наблюдением.
                start_probation(chat_id, user, score, reasons)
            elif v == "watch":
                start_probation(chat_id, user, score, reasons)
                if _join_notifications_allowed(chat_id, raid):
                    await notify_panel(event_card("👀 Под наблюдением", user,
                                                  reason=f"скор {score}: {'; '.join(reasons[:4])}"))

        steps = build_questions()
        first = steps[0]
        intro = captcha_caption(user, first["q"])
        if first.get("kind") == "image":
            # Только текст (чтобы новичок ввёл код), медиа/ссылки запрещены.
            await bot.restrict_chat_member(chat_id, user.id, permissions=TEXT_ONLY)
            photo = BufferedInputFile(make_captcha_image(first["answer"]), "captcha.png")
            sent = await bot.send_photo(chat_id, photo, caption=intro,
                                        reply_markup=captcha_photo_kb())
        else:
            await bot.restrict_chat_member(chat_id, user.id, permissions=MUTE)
            sent = await bot.send_message(chat_id, intro,
                                          reply_markup=captcha_markup(0, options_for(first)))
        task = asyncio.create_task(captcha_timeout(chat_id, user.id))
        pending[key] = {"steps": steps, "idx": 0, "msg_id": sent.message_id, "task": task}
        stats["challenged"] += 1
        log.info("Новичок %s (%s) — капча выдана", user.id, user.full_name)
    except TelegramBadRequest as e:
        log.warning("Не смог выдать капчу %s: %s", user.id, e)
        pending.pop(key, None)
    except Exception as e:
        log.exception("Ошибка в challenge для %s: %s", user.id, e)
        pending.pop(key, None)


@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    """Авто-приём заявок на вступление (мат/стоп-слова в имени -> отклонить)."""
    chat_id, user = req.chat.id, req.from_user
    log.info("Заявка на вступление: %s (%s) в чат %s", user.id, user.full_name, chat_id)
    pending_requests.setdefault(chat_id, set()).add(user.id)  # запомнили для /clearrequests

    async def _decline(reason_audit=None):
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except TelegramBadRequest:
            pass
        pending_requests.get(chat_id, set()).discard(user.id)
        if reason_audit:
            audit("DC-фильтр" if reason_audit.startswith("DC") else "заявки", reason_audit,
                  user.id, user.full_name)

    # Локдаун или DC-фильтр — отклоняем заявку сразу.
    if flag("LOCKDOWN"):
        await _decline()
        return
    if config.DC_BLOCK and flag("DC_CHECK_JOIN"):
        dc = await user_dc(user.id)
        if dc in config.DC_BLOCK:
            await _decline(f"DC-деклайн заявки DC{dc}")
            return

    if not flag("AUTO_ACCEPT"):
        return  # оставляем заявку в очереди (pending_requests) — очистить: /clearrequests
    if flag("CHECK_JOIN_NAMES"):
        public, why, hidden = name_check(f"{user.full_name or ''} {user.username or ''}")
        if public:
            if hidden:
                await _decline("отклонена по имени")   # без настоящего слова в журнале
                await notify_hidden(user, why)
            else:
                await _decline(f"отклонена по имени: {why}")
            await report(chat_id, f"🚫 Заявка отклонена: {mention(user)} — {esc(public)}.")
            return
    try:
        await bot.approve_chat_join_request(chat_id, user.id)
        pending_requests.get(chat_id, set()).discard(user.id)
        audit("заявки", "принята", user.id, user.full_name)
    except TelegramBadRequest as e:
        log.warning("Не смог принять заявку %s: %s", user.id, e)
        return

    # Антирейд: слишком много принятых заявок за короткое время -> выключить автоприём.
    buf = accept_burst.setdefault(chat_id, deque(maxlen=200))
    buf.append(now())
    cut = now() - timedelta(seconds=num("ACCEPT_BURST_WINDOW"))
    while buf and buf[0] < cut:
        buf.popleft()
    if len(buf) > num("ACCEPT_BURST_LIMIT"):
        buf.clear()
        storage.set_flag("AUTO_ACCEPT", False)
        stats["raids"] += 1
        audit("антирейд", "автоприём авто-выключен (всплеск заявок)", 0)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🧹 Отклонить все заявки",
                                 callback_data=f"clearreq:{chat_id}")]])
        await report(chat_id, "🚨 Всплеск заявок — <b>автоприём выключен автоматически</b>. "
                              "Новые заявки ждут ручного одобрения. Похоже на рейд — "
                              "рекомендую /lockdown on.", kb)
        await notify_panel(f"🚨 В чате <code>{chat_id}</code> всплеск заявок "
                           f"(>{num('ACCEPT_BURST_LIMIT')} за {num('ACCEPT_BURST_WINDOW')}с). "
                           "Автоприём выключен.")
        await enter_crisis(chat_id, "всплеск заявок на вступление")


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_joined(event: ChatMemberUpdated):
    await challenge(event.chat.id, event.new_chat_member.user)


@dp.message(F.new_chat_members)
async def on_new_members(message: Message):
    chat_id = message.chat.id
    # Во время рейда/локдауна/ЧС не показываем в чате служебные «X вошёл» —
    # чистим их принудительно, даже если удаление входов в настройках выключено.
    raid_now = (flag("LOCKDOWN")
                or (raid_until.get(chat_id) is not None and raid_until[chat_id] > now())
                or crisis_active(chat_id))
    if config.DELETE_JOIN_MESSAGE or flag("DELETE_SERVICE_MESSAGES") or raid_now:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    for user in message.new_chat_members:
        await challenge(message.chat.id, user)


@dp.message(F.left_chat_member)
async def on_left_member(message: Message):
    if flag("DELETE_SERVICE_MESSAGES"):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass


async def _advance_after_image(chat_id: int, user, st: dict) -> None:
    """Верный код на фото-шаге -> следующий (кнопочный) шаг или завершение проверки."""
    new_idx = st["idx"] + 1
    if st.get("msg_id"):                      # убрать сообщение с картинкой
        try:
            await bot.delete_message(chat_id, st["msg_id"])
        except TelegramBadRequest:
            pass
    if new_idx >= len(st["steps"]):           # картинка была единственным шагом
        await cleanup(chat_id, user.id, delete_msg=False)
        await finish_captcha(chat_id, user)
        return
    st["idx"] = new_idx
    nstep = st["steps"][new_idx]
    sent = await bot.send_message(
        chat_id, f"✅ Верно!\n\n{mention(user)}, {nstep['q']}",
        reply_markup=captcha_markup(new_idx, options_for(nstep)))
    st["msg_id"] = sent.message_id


async def handle_captcha_message(msg: Message, st: dict) -> None:
    """Сообщение новичка во время капчи: чистим чат, на фото-шаге сверяем введённый код."""
    chat_id, user = msg.chat.id, msg.from_user
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    step = st["steps"][st["idx"]]
    if step.get("kind") != "image":
        return                                # на кнопочных шагах любой текст просто удаляем
    given = re.sub(r"\D", "", msg.text or "")
    if not given:
        return                                # не цифры — ждём код дальше
    if given != step["answer"]:
        stats["failed"] += 1
        await ban_user(chat_id, user.id)
        audit("капча", "ban (неверный код)", user.id, user.full_name)
        await cleanup(chat_id, user.id)
        return
    await _advance_after_image(chat_id, user, st)


@dp.callback_query(F.data.startswith("cap:"))
async def on_captcha_answer(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    key = (chat_id, user_id)

    state = pending.get(key)
    if not state or not state.get("steps"):
        await cb.answer("Это не твоя капча 🙂")
        return

    try:
        _, idx_str, value = cb.data.split(":", 2)
        idx = int(idx_str)
    except ValueError:
        await cb.answer()
        return

    if idx != state["idx"]:
        await cb.answer("Кнопка устарела, отвечай на текущий вопрос.")
        return

    step = state["steps"][idx]
    if value != step["answer"]:
        await cb.answer("Неверно. Бан.", show_alert=True)
        stats["failed"] += 1
        await ban_user(chat_id, user_id)
        audit("капча", "ban (неверный ответ)", user_id, cb.from_user.full_name)
        await cleanup(chat_id, user_id)
        return

    new_idx = idx + 1
    if new_idx >= len(state["steps"]):
        await cb.answer("Проверка пройдена ✅")
        await cleanup(chat_id, user_id)
        await finish_captcha(chat_id, cb.from_user)
        return

    state["idx"] = new_idx
    nstep = state["steps"][new_idx]
    text = f"✅ Верно!\n\n{mention(cb.from_user)}, {nstep['q']}"
    try:
        await cb.message.edit_text(text, reply_markup=captcha_markup(new_idx, options_for(nstep)))
    except TelegramBadRequest:
        pass
    await cb.answer("Верно ✅")


@dp.callback_query(F.data == "capnew")
async def on_captcha_new(cb: CallbackQuery):
    """Заменить картинку фото-капчи (если цифры неразборчивы) — новый код + сброс таймера."""
    chat_id = cb.message.chat.id
    key = (chat_id, cb.from_user.id)
    st = pending.get(key)
    if not st or not st.get("steps"):
        await cb.answer("Это не твоя капча 🙂")
        return
    step = st["steps"][st["idx"]]
    if step.get("kind") != "image" or st.get("msg_id") != cb.message.message_id:
        await cb.answer("Кнопка неактуальна.")
        return
    step["answer"] = "".join(random.choice("0123456789") for _ in range(num("CAPTCHA_DIGITS")))
    photo = BufferedInputFile(make_captcha_image(step["answer"]), "captcha.png")
    try:
        await cb.message.edit_media(
            InputMediaPhoto(media=photo, caption=captcha_caption(cb.from_user, step["q"])),
            reply_markup=captcha_photo_kb())
    except TelegramBadRequest:
        pass
    old = st.get("task")                       # перезапустить таймаут
    if old and not old.done():
        old.cancel()
    st["task"] = asyncio.create_task(captcha_timeout(chat_id, cb.from_user.id))
    await cb.answer("Новая картинка 🔄")


# ---------------------------------------------------------- анализ картинок

def pick_image_file(message: Message):
    if message.photo:
        return message.photo[-1]
    if message.sticker:
        st = message.sticker
        return None if (st.is_animated or st.is_video) else st
    if message.document:
        mt = message.document.mime_type or ""
        if not mt.startswith("image/"):
            return None
        if (message.document.file_size or 0) > MAX_DOC_BYTES:
            return None
        return message.document
    return None


async def handle_violation(message: Message, reason: str) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id
    key = (chat_id, user_id)

    last = flagged.get(key)
    if last and (now() - last).total_seconds() < 30:
        await delayed_purge(chat_id, user_id, delay=3.0)
        return
    flagged[key] = now()

    await mute_user(chat_id, user_id)
    deleted = await purge_recent(chat_id, user_id)
    asyncio.create_task(delayed_purge(chat_id, user_id, delay=3.0))
    stats["img_muted"] += 1

    mins = config.PURGE_WINDOW_SECONDS // 60
    text = (
        f"🚫 {mention(message.from_user)}: {esc(reason)}.\n"
        f"Удалено сообщений за {mins} мин: <b>{deleted}</b>. Выдан <b>мут</b>.\n\n"
        f"Админ, проверь лог нарушения (Управление группой → Недавние действия) и реши:"
    )
    await report(chat_id, text, mod_keyboard(chat_id, user_id))
    if flag("NOTIFY_VIOLATIONS"):
        await notify_panel(event_card("🚨 Спам-картинка / 18+", message.from_user, reason=reason))
    log.info("МУТ %s — %s, удалено %d сообщ.", user_id, reason, deleted)


@dp.message((F.photo | F.sticker | F.document)
            & F.chat.type.in_({"group", "supergroup"}))
async def on_media(message: Message):
    if not message.from_user:
        return
    if (await is_admin(message.chat.id, message.from_user.id)
            or storage.is_trusted(message.chat.id, message.from_user.id)):
        return

    # Стикер из белого списка паков — доверенный, не гоняем через NSFW/гор
    # (детектор часто ложно срабатывает на обычные стикеры). Добавить пак:
    # ответить /allowpack на стикер (или /stickers в списке).
    if message.sticker and storage.is_pack_allowed(message.sticker.set_name):
        return

    file_obj = pick_image_file(message)
    if file_obj is None:
        return

    try:
        data = (await bot.download(file_obj)).read()
    except Exception as e:
        log.warning("Не смог скачать изображение: %s", e)
        return

    h = dhash_from_bytes(data)
    m = best_match(h) if h is not None else None
    if m and m[2] >= config.IMAGE_MATCH_PERCENT:
        name, _, percent = m
        await handle_violation(message, f"спам-картинка (похожесть {percent:.0f}% на {name})")
        return

    tag = f"{message.chat.id}_{message.message_id}"
    hit = await nsfw_check(data, tag)
    if hit:
        cls, score = hit
        await handle_violation(message, f"18+ контент ({cls}, {score:.0%})")
        return

    # 3) Шок-контент / гор (CLIP, если установлен и включён).
    if gore.available() and flag("GORE_ON"):
        g = await asyncio.to_thread(gore.detect, data, num("GORE_THRESHOLD_PCT") / 100)
        if g:
            label, score = g
            await handle_violation(message, f"шок-контент/гор ({score:.0%})")
            return

    # 4) Анти-деанон: OCR картинки на чужие персональные данные (скрин с
    #    телефоном/паспортом/адресом жертвы). По умолчанию — только у новичков.
    if flag("DEANON_ENABLED") and deanon.available():
        if not flag("DEANON_NEWCOMERS_ONLY") or _is_newcomer(message.chat.id, message.from_user.id):
            text = await asyncio.to_thread(
                deanon.extract_text, data, storage.get_str("DEANON_OCR_LANG", config.DEANON_OCR_LANG))
            if text:
                hit, why = deanon.scan_text(text, num("DEANON_MIN_HITS"))
                if hit:
                    await apply_punishment(
                        message, "деанон/угроза (себя задеанонь)",
                        action_for("DEANON_ACTION"),
                        audit_reason=f"на картинке — {why}")
                    if flag("NOTIFY_VIOLATIONS"):
                        await notify_panel(event_card("🕵 Анти-деанон: картинка",
                                                      message.from_user, reason=why))
                    return


def _is_newcomer(chat_id: int, user_id: int) -> bool:
    """Юзер считается новичком, пока действует медиа-карантин (или базовое окно 24ч)."""
    joined = newcomer.get((chat_id, user_id))
    if not joined:
        return False
    hrs = num("NEWCOMER_MEDIA_HOURS") or 24
    return (now() - joined).total_seconds() < hrs * 3600


@dp.callback_query(F.data.startswith("mod:"))
async def on_moderation(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        action, gid, uid = parts[1], int(parts[2]), int(parts[3])
        secs = int(parts[4]) if len(parts) > 4 else None
    except (ValueError, IndexError):
        await cb.answer()
        return

    # Право под конкретное действие кнопки (TG-админ проходит всегда через can()).
    _perm_for = {"ban": "ban", "banwipe": "ban", "mute": "mute",
                 "warn": "warn", "kick": "kick"}.get(action, "ban")
    if not await can(gid, cb.from_user.id, _perm_for):
        await cb.answer("Нет прав на это действие.", show_alert=True)
        return

    if action in ("ban", "mute", "banwipe"):
        reason = await _deny_target(gid, cb.from_user.id, uid)
        if reason:
            await cb.answer(reason, show_alert=True)
            return

    actor = f"админ {cb.from_user.full_name}"
    try:  # ник цели (чтобы было видно, КОГО наказали)
        _m = await bot.get_chat_member(gid, uid)
        tgt = id_mention(uid, _m.user.full_name)
    except TelegramBadRequest:
        tgt = id_mention(uid)
    if action == "ban":
        await ban_user(gid, uid)
        audit(actor, "ban", uid)
        await cb.answer("Забанен")
        result = f"🔨 {tgt} — бан. {mod_decision(cb.from_user)}"
    elif action == "banwipe":
        await ban_user(gid, uid)
        n = await purge_recent(gid, uid)
        audit(actor, "ban+чистка", uid)
        await cb.answer("Бан + чистка")
        result = f"🧹 {tgt} — бан + удалено {n} сообщ. {mod_decision(cb.from_user)}"
    elif action == "mute":
        await mute_user(gid, uid, secs)
        audit(actor, f"mute {human_duration(secs)}", uid)
        await cb.answer("Замучен")
        result = f"🔇 {tgt} — мут ({human_duration(secs)}). {mod_decision(cb.from_user)}"
    elif action == "unmute":
        try:
            await bot.restrict_chat_member(gid, uid, permissions=FULL)
        except TelegramBadRequest as e:
            log.warning("Не смог размутить %s: %s", uid, e)
        await cb.answer("Размучен")
        result = f"✅ {tgt} — размут. {mod_decision(cb.from_user)}"
    else:
        await cb.answer()
        return
    # Дописываем решение к исходной карточке (не затираем контекст с ником цели).
    base = cb.message.html_text if cb.message.html_text else ""
    text = f"{base}\n\n{result}" if base else result
    try:
        await cb.message.edit_text(text[:4000])
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "hide")
async def on_hide(cb: CallbackQuery):
    await cb.answer("Скрыто")
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("vrf:"))
async def on_verify_review(cb: CallbackQuery):
    """Ручное решение по «серому» номеру: Впустить / Отклонить."""
    parts = cb.data.split(":")
    try:
        decision, gid, uid = parts[1], int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        await cb.answer()
        return
    # Решать может админ чата, авторизованный в панели или владелец.
    presser = cb.from_user.id
    allowed = (presser in panel_auth or storage.is_owner(presser)
               or await is_admin(gid, presser))
    if not allowed:
        await cb.answer("Решать может только администратор.", show_alert=True)
        return

    pend = storage.get_pending_verify(uid) or {}
    prefix = pend.get("prefix", "")
    tail = pend.get("tail", "")
    try:
        _m = await bot.get_chat_member(gid, uid)
        user_obj = _m.user
        tgt = id_mention(uid, user_obj.full_name)
    except TelegramBadRequest:
        user_obj = None
        tgt = id_mention(uid)

    if decision == "ok":
        storage.set_phone_verified(uid, prefix or "?", tail, fmt_when())
        if user_obj is not None:
            await _release_after_verify(uid, user_obj)
        else:                                   # объект не достали — впустим по id
            await grant_full_or_quarantine(gid, uid)
            verify_wait.pop(uid, None)
            storage.clear_pending_verify(uid)
        await _safe_dm(uid, "✅ Модератор одобрил твою заявку. Доступ в чат открыт, добро пожаловать!")
        audit(f"админ {cb.from_user.full_name}", "верификация одобрена", uid)
        await cb.answer("Впущен")
        result = f"✅ {tgt} — впущен вручную. {mod_decision(cb.from_user)}"
    elif decision == "no":
        await _fail_verify(uid, user_obj, tail or "—")
        await _safe_dm(uid, "🚫 Модератор отклонил твою заявку на доступ к чату.")
        audit(f"админ {cb.from_user.full_name}", "верификация отклонена", uid)
        await cb.answer("Отклонён")
        result = f"🚫 {tgt} — отклонён. {mod_decision(cb.from_user)}"
    else:
        await cb.answer()
        return
    base = cb.message.html_text if cb.message.html_text else ""
    text = f"{base}\n\n{result}" if base else result
    try:
        await cb.message.edit_text(text[:4000])
    except TelegramBadRequest:
        pass


async def clear_requests(chat_id: int, actor_id: int) -> int:
    """Отклонить все заявки, которые бот видел (pending_requests)."""
    reqs = list(pending_requests.get(chat_id, set()))
    declined = 0
    for uid in reqs:
        try:
            await bot.decline_chat_join_request(chat_id, uid)
            declined += 1
        except TelegramBadRequest:
            pass
        pending_requests.get(chat_id, set()).discard(uid)
        await asyncio.sleep(0.2)  # троттлинг под лимиты
    audit("заявки", f"очистка: отклонено {declined}", actor_id)
    return declined


@dp.callback_query(F.data.startswith("clearreq:"))
async def on_clearreq(cb: CallbackQuery):
    try:
        gid = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not await can(gid, cb.from_user.id, "requests"):
        await cb.answer("Нужно право на заявки.", show_alert=True)
        return
    n = len(pending_requests.get(gid, set()))
    await cb.answer(f"Отклоняю {n} заявок…")
    declined = await clear_requests(gid, cb.from_user.id)
    try:
        suffix = " Решение модерации." if flag("ANON_ADMIN") else f" ({mod_name(cb.from_user)})"
        await cb.message.edit_text(cb.message.html_text +
                                   f"\n\n🧹 Отклонено заявок: {declined}.{suffix}")
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("crisisack:"))
async def on_crisis_ack(cb: CallbackQuery):
    """Модер нажал «Беру контроль» — останавливаем авто-эскалацию."""
    try:
        gid = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not await can(gid, cb.from_user.id, "mute"):
        await cb.answer("Только модератор.", show_alert=True)
        return
    st = crisis.get(gid)
    if not st:
        await cb.answer("ЧС уже завершён.", show_alert=True)
        return
    st["acked"] = True
    await cb.answer("Принято — ручное управление, авто-эскалация остановлена.")
    audit("ЧС-автопилот", "модер взял контроль", cb.from_user.id, cb.from_user.full_name)
    try:
        result = ("🛡 Контроль принят модерацией." if flag("ANON_ADMIN") else
                  f"🛡 Контроль взял {mod_name(cb.from_user)}.")
        await cb.message.edit_text(cb.message.html_text + f"\n\n{result}")
    except TelegramBadRequest:
        pass


@dp.message(Command("clearrequests", "clearreq"))
async def cmd_clearrequests(message: Message):
    if not await _can(message, "requests"):
        return
    chat_id = message.chat.id
    n = len(pending_requests.get(chat_id, set()))
    if not n:
        await message.answer("Очередь заявок пуста (бот видит только пришедшие ему апдейтом). "
                             "Для полной зачистки ВСЕХ висящих заявок — MTProto (purge_raid.py).")
        return
    await message.answer(f"⏳ Отклоняю {n} заявок… (медленно из-за лимитов)")
    declined = await clear_requests(chat_id, message.from_user.id)
    await message.answer(f"✅ Отклонено заявок: {declined}.")


# ------------------------------------------------ голосование /vb

def vote_kb(mid: int, yes_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"👍🏻 За ({yes_count})", callback_data=f"vote:yes:{mid}"),
        InlineKeyboardButton(text="👎🏻 Против", callback_data=f"vote:no:{mid}"),
    ]])


def vote_render(v: dict) -> str:
    lines = [
        f"👮🏻‍♂️ {v['starter_name']} запустил голосование",
        f"✂️👉🏻 Заглушение пользователя {id_mention(v['target'], v['tname'])}.",
        "Вы поддерживаете это решение?",
    ]
    if v["yes"]:
        lines.append("\nПроголосовали:")
        anonymous = v.get("anonymous_yes", set())
        for uid, name in v["yes"].items():
            if uid in anonymous:
                lines.append("👍🏻 Модерация")
            else:
                lines.append(f"👍🏻 {esc(name)} #{uid}")
    return "\n".join(lines)


@dp.message(Command("vb"))
async def cmd_vb(message: Message):
    """Запустить голосование за заглушение пользователя (ответом на него)."""
    global vote_seq
    is_staff = await _can(message, "mute")
    if not (is_staff or flag("VOTE_ANYONE")):   # обычным юзерам — только если разрешено
        return
    r = message.reply_to_message
    if not r or not r.from_user:
        await message.answer("Ответь /vb на сообщение пользователя — запущу голосование.")
        return
    target = r.from_user
    starter = message.from_user
    # Нельзя голосовать против себя, админов и персонала.
    reason = await _deny_target(message.chat.id, starter.id, target.id)
    if reason:
        await message.answer(reason)
        return
    # Антиспам: обычному пользователю — пауза между голосованиями.
    if not is_staff:
        key = (message.chat.id, starter.id)
        last = vb_cooldown.get(key)
        if last and (now() - last).total_seconds() < config.VOTE_COOLDOWN:
            left = int(config.VOTE_COOLDOWN - (now() - last).total_seconds()) + 1
            await message.answer(f"⏳ Подожди {left}с перед новым голосованием.")
            return
        vb_cooldown[key] = now()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    vote_seq += 1
    vid = vote_seq
    anonymous_starter = bool(flag("ANON_ADMIN") and is_staff)
    v = {"chat": message.chat.id, "target": target.id, "tname": target.full_name,
         "starter_name": "Модерация" if anonymous_starter else esc(starter.full_name),
         "yes": {starter.id: starter.full_name},
         "anonymous_yes": {starter.id} if anonymous_starter else set(),
         "no": set(), "done": False, "ts": now()}
    votes[vid] = v
    # Отправляем СРАЗУ с клавиатурой (по vote_id) — кнопки не теряются.
    await bot.send_message(message.chat.id, vote_render(v),
                           reply_markup=vote_kb(vid, len(v["yes"])),
                           disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("vote:"))
async def on_vote(cb: CallbackQuery):
    try:
        _, choice, mid_s = cb.data.split(":")
        mid = int(mid_s)
    except ValueError:
        await cb.answer()
        return
    v = votes.get(mid)
    if not v or v["done"]:
        await cb.answer("Голосование завершено.")
        return
    uid = cb.from_user.id

    if choice == "no":
        if await can(v["chat"], uid, "ban"):         # 👎 админа/персонала с правом = вето, отмена
            v["done"] = True
            votes.pop(mid, None)
            await cb.answer("Голосование отменено.")
            try:
                text = ("❌ Голосование отменено модерацией." if flag("ANON_ADMIN") else
                        f"❌ Голосование отменено админом {mod_name(cb.from_user)}.")
                await cb.message.edit_text(text)
            except TelegramBadRequest:
                pass
            return
        v["no"].add(uid)
        v["yes"].pop(uid, None)
        v.setdefault("anonymous_yes", set()).discard(uid)
    else:                                             # 👍 (в т.ч. админский — обычный голос)
        v["yes"][uid] = cb.from_user.full_name
        v["no"].discard(uid)
        anonymous = v.setdefault("anonymous_yes", set())
        if flag("ANON_ADMIN") and await is_staff_user(v["chat"], uid):
            anonymous.add(uid)
        else:
            anonymous.discard(uid)

    if len(v["yes"]) >= config.VOTE_LIMIT and not v["done"]:
        v["done"] = True
        if config.VOTE_ACTION == "ban":
            await ban_user(v["chat"], v["target"])
            verb = "забанен"
        else:
            await mute_user(v["chat"], v["target"])
            verb = "заглушён"
        audit("голосование", f"{config.VOTE_ACTION} ({len(v['yes'])} за)", v["target"], v["tname"])
        await cb.answer("Решение принято")
        try:
            await cb.message.edit_text(f"✅ По итогам голосования ({len(v['yes'])} 👍🏻) "
                                       f"{id_mention(v['target'], v['tname'])} {verb}.")
        except TelegramBadRequest:
            pass
        votes.pop(mid, None)
        return

    await cb.answer("Голос учтён")
    try:
        await cb.message.edit_text(vote_render(v), reply_markup=vote_kb(mid, len(v["yes"])))
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------- команды

async def _staff_only(message: Message, perm: str | None = None) -> bool:
    """Пускает TG-админа ИЛИ носителя внутренней роли (в т.ч. без TG-админки).

    perm=None — достаточно быть персоналом (для ЧТЕНИЯ: /info, /stats, /log…);
    perm задан — нужно это конкретное право (как _can, наказания/настройки).
    Владелец бота проходит всегда.
    """
    if not message.from_user:
        return False
    uid = message.from_user.id
    if perm is not None:
        if storage.is_owner(uid):
            return True
        return await can(message.chat.id, uid, perm)
    return await is_staff_user(message.chat.id, uid)


async def staff_reply(message: Message, text: str) -> None:
    """Приватный ответ на служебную команду.

    В группе: шлём вызвавшему в ЛС и удаляем саму команду из чата, чтобы данные
    админов/траст/журнал не палились на весь чат. Если ЛС закрыта (юзер не
    запускал бота) — обезличенная подсказка. В личке бота — обычный ответ.
    """
    if message.chat.type == "private":
        await message.answer(text)
        return
    uid = message.from_user.id if message.from_user else None
    delivered = False
    if uid:
        try:
            await bot.send_message(uid, text)
            delivered = True
        except (TelegramBadRequest, TelegramForbiddenError):
            delivered = False
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    if not delivered:
        try:
            await message.answer(
                "🔒 Напиши мне в ЛС <b>/start</b> — пришлю ответ туда, не в чат.")
        except TelegramBadRequest:
            pass


def _mention_uid(message: Message):
    """id из entity text_mention (тап по юзеру без @ника) в тексте команды."""
    for e in (getattr(message, "entities", None) or []):
        if getattr(e, "type", None) == "text_mention" and getattr(e, "user", None):
            uname_cache_add(e.user)
            return e.user.id
    return None


def uname_cache_add(user) -> None:
    """Запомнить @ник -> id (для последующего таргета по нику)."""
    if getattr(user, "username", None):
        uname_cache[user.username.lower()] = user.id


def _resolve_username(token: str):
    """'@ник' или 'ник' -> user_id из кэша (кого бот уже видел), иначе None."""
    name = token.lstrip("@").lower()
    return uname_cache.get(name)


def _is_target_token(token: str) -> bool:
    """Похоже ли слово на указание цели: числовой id или @ник."""
    return token.startswith("@") or token.lstrip("-").isdigit()


def _resolve_target_token(token: str):
    """'<id>' -> int, '@ник' -> uid из кэша, иначе None."""
    if token.startswith("@"):
        return _resolve_username(token)
    if token.lstrip("-").isdigit():
        return int(token)
    return None


def _target_id(message: Message):
    r = message.reply_to_message
    if r and r.from_user:
        uname_cache_add(r.from_user)
        return r.from_user.id
    mid = _mention_uid(message)
    if mid is not None:
        return mid
    parts = (message.text or "").split()
    if len(parts) > 1 and _is_target_token(parts[1]):
        uid = _resolve_target_token(parts[1])
        return 0 if uid is None else uid    # 0 = @ник, которого бот не видел
    return None


_UNIT_WORDS = ("нед", "недел", "дн", "ден", "дня", "дне", "дней", "день",
               "час", "часа", "часов", "мин", "минут", "сек")


def _is_unit(w: str) -> bool:
    w = w.lower()
    return w in ("ч", "м", "с", "d", "h", "m", "w", "s") or any(w.startswith(p) for p in _UNIT_WORDS)


def _split_dur_reason(tokens: list):
    """Из хвоста команды -> (seconds|None, reason). Длительность только в начале."""
    if not tokens:
        return None, ""
    m = re.match(r"^(\d+)([а-яёa-z]+)$", tokens[0], re.I)  # склеенное «3ч», «3дня»
    if m and _is_unit(m.group(2)):
        return parse_duration(tokens[0]), " ".join(tokens[1:]).strip()
    if not tokens[0].lstrip("-").isdigit():
        return None, " ".join(tokens).strip()          # нет числа -> всё причина
    if len(tokens) >= 2 and _is_unit(tokens[1]):        # «3 дня причина»
        return parse_duration(tokens[0] + " " + tokens[1]), " ".join(tokens[2:]).strip()
    return parse_duration(tokens[0]), " ".join(tokens[1:]).strip()  # «3 причина» (число=часы)


def _target_dur_reason(message: Message):
    """(uid, seconds|None, reason) — для /ban /mute.

    Цель: ответ на сообщение, либо первым аргументом — числовой id / @ник /
    text_mention. Дальше — необязательные срок и причина.
    Вернёт uid=None если цель не распознана; uid=0 (спец-код) если @ник ещё
    не встречался боту (чтобы отличить «не понял» от «не знаю такого ника»).
    """
    r = message.reply_to_message
    parts = (message.text or "").split()
    if r and r.from_user:
        uname_cache_add(r.from_user)
        secs, reason = _split_dur_reason(parts[1:])
        return r.from_user.id, secs, reason
    mid = _mention_uid(message)
    if mid is not None:
        secs, reason = _split_dur_reason(parts[1:])   # entity сам не даёт токена — весь хвост
        return mid, secs, reason
    if len(parts) > 1 and _is_target_token(parts[1]):
        uid = _resolve_target_token(parts[1])
        if uid is None:                               # @ник, которого бот не видел
            return 0, None, ""
        secs, reason = _split_dur_reason(parts[2:])
        return uid, secs, reason
    return None, None, ""


_UNKNOWN_UNAME = ("🤷 Не знаю такого @ника — бот его ещё не видел в этом чате. "
                  "Ответь командой на его сообщение или укажи числовой id.")
_NO_TARGET = ("Ответь командой на пользователя или укажи id/@ник "
              "(бот должен был видеть его сообщения).")


async def _need_target(message: Message, uid, hint: str = _NO_TARGET) -> bool:
    """True — цель не годится (уже ответил пользователю), надо выйти из хэндлера.

    uid: None — цель не распознана; 0 — @ник неизвестен боту; иначе валидный id.
    """
    if uid is None:
        await message.answer(hint)
        return True
    if uid == 0:
        await message.answer(_UNKNOWN_UNAME)
        return True
    return False


@dp.message(Command("spam"))
async def cmd_spam(message: Message):
    if not await _staff_only(message, "manage"):
        return
    reply = message.reply_to_message
    if not reply:
        await message.answer("Ответь этой командой на сообщение с картинкой-спамом.")
        return
    file_obj = pick_image_file(reply)
    if file_obj is None:
        await message.answer("В том сообщении нет подходящей картинки.")
        return
    try:
        data = (await bot.download(file_obj)).read()
    except Exception as e:
        await message.answer(f"Не смог скачать картинку: {e}")
        return
    h = dhash_from_bytes(data)
    if h is None:
        await message.answer("Не смог обработать это изображение.")
        return
    fname = f"spam_{reply.message_id}.jpg"
    try:
        with open(os.path.join(photo_dir(), fname), "wb") as f:
            f.write(data)
    except OSError as e:
        log.warning("Не смог сохранить эталон: %s", e)
    ref_hashes.append((fname, h))
    punished = ""
    if reply.from_user and not await is_admin(message.chat.id, reply.from_user.id):
        await handle_violation(reply, "картинка отмечена админом как спам")
        punished = " Автор замучен, спам вычищен."
    await message.answer(f"✅ В базе спама теперь {len(ref_hashes)}.{punished}")


@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    if not await _staff_only(message, "manage"):
        return
    load_reference_hashes()
    await message.answer(f"🔄 База перезагружена: {len(ref_hashes)} картинок.")


def _pack_from_message(message: Message):
    """Имя стикерпака: из ответа на стикер, либо аргументом (имя пака/ссылка t.me/addstickers/…)."""
    r = message.reply_to_message
    if r and r.sticker and r.sticker.set_name:
        return r.sticker.set_name
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) > 1:
        val = arg[1].strip()
        # поддержим ссылку вида https://t.me/addstickers/PackName
        m = re.search(r"addstickers/([A-Za-z0-9_]+)", val)
        return m.group(1) if m else val.lstrip("@")
    return None


@dp.message(Command("allowpack"))
async def cmd_allowpack(message: Message):
    """Добавить стикерпак в белый список (его стикеры не проверяются на 18+)."""
    if not await _staff_only(message, "manage"):
        return
    pack = _pack_from_message(message)
    if not pack:
        await message.answer("Ответь /allowpack на стикер из нужного пака "
                             "или укажи имя пака: /allowpack ИмяПака (или ссылку addstickers/…).")
        return
    if storage.allow_pack(pack):
        await message.answer(f"✅ Пак <code>{esc(pack)}</code> в белом списке — "
                             "его стикеры больше не триггерят 18+-детектор.")
    else:
        await message.answer(f"ℹ️ Пак <code>{esc(pack)}</code> уже в белом списке.")


@dp.message(Command("denypack"))
async def cmd_denypack(message: Message):
    """Убрать стикерпак из белого списка (снова проверять на 18+)."""
    if not await _staff_only(message, "manage"):
        return
    pack = _pack_from_message(message)
    if not pack:
        await message.answer("Ответь /denypack на стикер или укажи имя пака: /denypack ИмяПака.")
        return
    if storage.disallow_pack(pack):
        await message.answer(f"🚫 Пак <code>{esc(pack)}</code> убран из белого списка — "
                             "снова проверяется на 18+.")
    else:
        await message.answer(f"ℹ️ Пака <code>{esc(pack)}</code> не было в белом списке.")


@dp.message(Command("stickers", "packs"))
async def cmd_stickers(message: Message):
    """Показать белый список стикерпаков."""
    if not await _staff_only(message):
        return
    packs = storage.sticker_packs()
    if not packs:
        await message.answer("Белый список стикерпаков пуст.\n"
                             "Добавить: ответь /allowpack на стикер (или /allowpack ИмяПака).")
        return
    lines = ["🧷 <b>Белые стикерпаки</b> (не проверяются на 18+):"]
    for p in packs:
        lines.append(f"• <code>{esc(p)}</code>")
    lines.append("\nУбрать: /denypack ИмяПака (или ответом на стикер).")
    await message.answer("\n".join(lines))


@dp.message(Command("reloadgore"))
async def cmd_reloadgore(message: Message):
    """Подгрузить гор-детектор на лету (после установки torch/transformers)."""
    if not await _staff_only(message, "manage"):
        return
    if gore.available():
        await message.answer("Гор-детектор уже загружен ✅")
        return
    await message.answer("⏳ Загружаю гор-детектор (может качать модель ~600 МБ)…")
    await asyncio.to_thread(gore.load, config.GORE_MODEL)
    await message.answer(f"Результат: {esc(gore.status())}")


@dp.message(Command("check", "checkgore"))
async def cmd_check(message: Message):
    """Диагностика: ответом на картинку показать баллы хеша/18+/гора."""
    if not await _staff_only(message):
        return
    reply = message.reply_to_message
    file_obj = pick_image_file(reply) if reply else None
    if file_obj is None:
        await message.answer("Ответь /check на сообщение с картинкой.")
        return
    try:
        data = (await bot.download(file_obj)).read()
    except Exception as e:
        await message.answer(f"Не смог скачать: {e}")
        return

    h = dhash_from_bytes(data)
    m = best_match(h) if h is not None else None
    hashline = f"{m[2]:.0f}% на {esc(m[0])}" if m else "база пуста/нет совпадений"

    prob = await nsfw_debug(data, f"chk_{reply.message_id}")
    if prob is None:
        nsfwline = "детектор не загружен"
    else:
        mark = "🔴 БАН" if prob >= config.NSFW_THRESHOLD else "🟢 чисто"
        nsfwline = f"18+ {prob:.0%} — {mark} (порог {config.NSFW_THRESHOLD:.0%})"

    if gore.available():
        g = await asyncio.to_thread(gore.detect, data, 0.0)
        goreline = f"вероятность гора {g[1]:.0%}" if g else "—"
    else:
        goreline = "—"

    await message.answer(
        "🔎 <b>Проверка картинки</b>\n"
        f"Хеш-база: {hashline} (порог {config.IMAGE_MATCH_PERCENT}%)\n"
        f"18+ (ViT): {nsfwline}\n"
        f"Гор-детектор: {esc(gore.status())} | проверка: {'вкл' if flag('GORE_ON') else 'выкл'}\n"
        f"Гор: {goreline} (порог {num('GORE_THRESHOLD_PCT')}%)\n\n"
        "Если детектор не загружен или что-то не так — команда /diag."
    )


@dp.message(Command("deanon"))
async def cmd_deanon(message: Message):
    """Диагностика анти-деанона: ответом на картинку показать OCR-текст и найденные данные."""
    if not await _staff_only(message, "manage"):
        return
    if not deanon.available():
        await staff_reply(message, f"Анти-деанон OCR: {esc(deanon.status())}.\n"
                             "Установи движок: <code>pip install rapidocr-onnxruntime</code>, "
                             "затем перезапусти бота.")
        return
    reply = message.reply_to_message
    file_obj = pick_image_file(reply) if reply else None
    if file_obj is None:
        await staff_reply(message, "Ответь /deanon на сообщение с картинкой.")
        return
    try:
        data = (await bot.download(file_obj)).read()
    except Exception as e:
        await message.answer(f"Не смог скачать: {e}")
        return
    text = await asyncio.to_thread(
        deanon.extract_text, data, storage.get_str("DEANON_OCR_LANG", config.DEANON_OCR_LANG))
    hit, types = deanon.is_deanon(text or "", num("DEANON_MIN_HITS"))
    mark = "🔴 деанон" if hit else "🟢 чисто"
    snippet = (text or "").strip()
    snippet = snippet[:500] + "…" if len(snippet) > 500 else snippet
    await staff_reply(message,
        "🕵 <b>Проверка на деанон</b>\n"
        f"OCR-движок: {esc(deanon.status())}\n"
        f"Найдено: {esc(deanon.describe(types)) or '—'}\n"
        f"Вердикт: {mark} (нужно типов: {num('DEANON_MIN_HITS')}, действие: {action_for('DEANON_ACTION')})\n\n"
        f"Распознанный текст:\n<code>{esc(snippet) or '(пусто)'}</code>")


@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    """Полная самодиагностика: ИИ-модели, апдейты, права бота в этом чате."""
    if not await _staff_only(message):
        return
    # torch
    try:
        import torch
        torch_line = f"✅ torch {torch.__version__}"
    except Exception as e:
        torch_line = f"❌ {type(e).__name__}: {e}"
    # transformers
    try:
        import transformers
        tr_line = f"✅ transformers {transformers.__version__}"
    except Exception as e:
        tr_line = f"❌ {type(e).__name__}: {e}"

    updates = ", ".join(dp.resolve_used_update_types())

    # права бота в текущем чате
    rights = "не в группе"
    if message.chat.type in ("group", "supergroup"):
        try:
            me = await bot.get_me()
            cm = await bot.get_chat_member(message.chat.id, me.id)
            st = str(cm.status)
            if st == "administrator":
                rights = (f"админ | бан:{'✅' if cm.can_restrict_members else '❌'} "
                          f"удаление:{'✅' if cm.can_delete_messages else '❌'} "
                          f"приём заявок:{'✅' if cm.can_invite_users else '❌'}")
            else:
                rights = f"⚠️ НЕ админ ({st}) — модерация не работает"
        except TelegramBadRequest as e:
            rights = f"не смог проверить: {e}"

    await staff_reply(message,
        "🩺 <b>Диагностика</b>\n"
        f"<b>Картинки/ИИ:</b>\n"
        f"• {torch_line}\n• {tr_line}\n"
        f"• Гор (CLIP): {esc(gore.status())} | GORE_ENABLED={config.GORE_ENABLED}\n"
        f"• Детектор 18+ (ViT {config.NSFW_MODEL}): {esc(nsfwvit.status())} "
        f"(порог {config.NSFW_THRESHOLD:.0%}) | NSFW_ENABLED={config.NSFW_ENABLED}\n"
        f"• Анти-деанон OCR: {esc(deanon.status())} | DEANON_ENABLED={config.DEANON_ENABLED}\n"
        f"• Эталонов в базе: {len(ref_hashes)}\n\n"
        f"<b>Заявки/апдейты:</b>\n"
        f"• Автоприём (AUTO_ACCEPT): {'вкл' if flag('AUTO_ACCEPT') else 'выкл'}\n"
        f"• Подписка на апдейты: {esc(updates)}\n"
        f"• chat_join_request в подписке: "
        f"{'✅' if 'chat_join_request' in updates else '❌'}\n\n"
        f"<b>Права бота в этом чате:</b>\n• {esc(rights)}\n\n"
        "Если «приём заявок ❌» — выдай боту право «Добавление участников» и включи "
        "в настройках группы «Заявки на вступление». Гор не загружен → /check покажет ошибку, "
        "ставь torch+transformers (requirements-gore.txt)."
    )


@dp.message(Command("lockdown"))
async def cmd_lockdown(message: Message):
    """Паника: банить ВСЕХ входящих без капчи. /lockdown on|off."""
    if not await _can(message, "requests"):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"🚨 Локдаун: {'ВКЛ' if flag('LOCKDOWN') else 'выкл'}. "
                             "/lockdown on — банить всех входящих (при рейде), /lockdown off — снять.")
        return
    storage.set_flag("LOCKDOWN", v)
    await message.answer("🚨 ЛОКДАУН ВКЛЮЧЁН — все входящие банятся без капчи."
                         if v else "✅ Локдаун снят, обычный режим.")


@dp.message(Command("crisis"))
async def cmd_crisis(message: Message):
    """Автономный ЧС-режим: статус и ручной вход/выход. /crisis on|off."""
    if not await _can(message, "requests"):
        return
    chat_id = message.chat.id
    v = _parse_onoff(message)
    if v is None:
        st = crisis.get(chat_id)
        if st and crisis_active(chat_id):
            left = int((st["until"] - now()).total_seconds())
            lvl = "усиление" if st["level"] < 2 else "автобан входящих"
            ack = "модер на связи" if st["acked"] else "модеры не ответили"
            await message.answer(
                f"🚨 <b>ЧС-режим активен</b>\n"
                f"Причина: {esc(st['reason'])}\n"
                f"Уровень: {st['level']} ({lvl}) — {ack}\n"
                f"Затишье до авто-выхода: ~{max(0, left)}с\n\n"
                f"Автопилот {'ВКЛ' if flag('AUTO_CRISIS_ENABLED') else 'выкл'}. "
                f"/crisis off — снять вручную.")
        else:
            await message.answer(
                f"🟢 Сейчас всё спокойно. Автопилот ЧС: "
                f"{'ВКЛ' if flag('AUTO_CRISIS_ENABLED') else 'выкл'}.\n"
                f"Ловит рейд по 3 сигналам (входы/заявки/волна сообщений), сам "
                f"усиливает защиту и откатывает. /crisis on — включить вручную.")
        return
    if v:
        reason = "вручную (модерация)" if flag("ANON_ADMIN") else f"вручную ({mod_name(message.from_user)})"
        await enter_crisis(chat_id, reason, force=True)
        await message.answer("🚨 ЧС-режим включён вручную.")
    else:
        if crisis_active(chat_id):
            await exit_crisis(chat_id, manual=True)
        else:
            await message.answer("ЧС-режим и так не активен.")


@dp.message(Command("checkdc"))
async def cmd_checkdc(message: Message):
    """Показать датацентр пользователя (ответом или по id)."""
    if not await _can(message, "requests"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid, "Ответь /checkdc на пользователя или укажи id/@ник."):
        return
    dc = await user_dc(uid)
    if dc is None:
        await message.answer("DC не определить (нет фото или скрыто приватностью).")
    else:
        flag_bad = " ⚠️ в списке блокировки" if dc in config.DC_BLOCK else ""
        await message.answer(f"Пользователь <code>{uid}</code>: <b>DC{dc}</b>{flag_bad}.")


@dp.message(Command("purgedc"))
async def cmd_purgedc(message: Message):
    """Вычистить из чата DC-блок среди тех, кого бот видел (новички/писавшие)."""
    if not await _can(message, "requests"):
        return
    if not config.DC_BLOCK:
        await message.answer("DC_BLOCK пуст — нечего чистить.")
        return
    chat_id = message.chat.id
    # кандидаты: кого бот видел входящим или писавшим в этом чате
    cands = {u for (c, u) in newcomer if c == chat_id}
    cands |= {u for (c, u) in recent if c == chat_id}
    cands -= await get_admins(chat_id)
    arg = (message.text or "").split()
    confirm = len(arg) > 1 and arg[1].lower() in ("confirm", "go", "да")

    if not confirm:
        await message.answer(
            f"🔎 Кандидатов на проверку DC (видел бот): <b>{len(cands)}</b>.\n"
            f"Блокируемые DC: {config.DC_BLOCK}.\n"
            "Это лишь те, кого бот успел увидеть — НЕ все 2к. Для полной зачистки нужен "
            "MTProto-скрипт purge_raid.py.\n\n"
            "Запустить бан здесь: <code>/purgedc confirm</code>")
        return

    await message.answer(f"⏳ Проверяю {len(cands)} аккаунтов по DC… (медленно из-за лимитов)")
    banned = 0
    for uid in cands:
        dc = await user_dc(uid)
        if dc in config.DC_BLOCK:
            await ban_user(chat_id, uid)
            banned += 1
        await asyncio.sleep(0.3)  # троттлинг под лимиты Telegram
    audit("DC-purge", f"бан {banned} (DC{config.DC_BLOCK})", message.from_user.id)
    await message.answer(f"✅ Забанено по DC: <b>{banned}</b> из {len(cands)} проверенных.")


@dp.message(Command("cleandeleted", "cleandel"))
async def cmd_cleandeleted(message: Message):
    """Выкинуть из чата удалённые аккаунты (Deleted Account) среди виденных ботом."""
    if not await _can(message, "ban"):
        return
    chat_id = message.chat.id
    cands = known_members(chat_id) - await get_admins(chat_id)
    arg = (message.text or "").split()
    confirm = len(arg) > 1 and arg[1].lower() in ("confirm", "go", "да")

    if not confirm:
        extra = ""
        if userbot.available():
            extra = ("\n\n💡 Доступен юзербот — <code>/scanall</code> пройдёт по "
                     "ВСЕМ участникам, а не только виденным.")
        await message.answer(
            f"🔎 Кандидатов к проверке (видел бот): <b>{len(cands)}</b>.\n"
            "Bot API видит только тех, кто писал/вступал — НЕ весь список группы.\n"
            "Запустить чистку: <code>/cleandeleted confirm</code>" + extra)
        return

    await message.answer(f"⏳ Проверяю {len(cands)} аккаунтов на удалёнку… "
                         "(медленно из-за лимитов Telegram)")
    res = await sweep_deleted(chat_id, do_kick=True)
    audit("чистка", f"кикнуто удалёнок {res['kicked']}", message.from_user.id)
    await message.answer(
        f"✅ Готово. Проверено: <b>{res['scanned']}</b>, "
        f"удалёнок: <b>{res['deleted']}</b>, выкинуто: <b>{res['kicked']}</b>.")


@dp.message(Command("scanall"))
async def cmd_scanall(message: Message):
    """Полный скан ВСЕХ участников через юзербот (Telethon) и кик удалёнок."""
    if not await _can(message, "ban"):
        return
    if not userbot.available():
        await message.answer(
            "🚫 Юзербот не настроен.\n"
            f"Статус: {esc(userbot.status())}.\n"
            "Нужны Telethon + user-сессия (см. userbot.py и config.USERBOT_*). "
            "Пока доступна чистка виденных: /cleandeleted.")
        return
    await message.answer("⏳ Полный скан участников через юзербот… это может занять время.")
    try:
        res = await userbot.scan_deleted(message.chat.id, kick=True)
    except Exception as e:                        # noqa: BLE001
        await message.answer(f"Юзербот упал: {esc(str(e))}")
        return
    if res.get("error"):
        await message.answer(f"⚠️ {esc(res['error'])}")
        return
    stats["deleted_kicked"] += res["kicked"]
    audit("чистка", f"юзербот: кикнуто {res['kicked']}", message.from_user.id)
    await message.answer(
        f"✅ Юзербот прошёл всех.\nУчастников: <b>{res['scanned']}</b>, "
        f"удалёнок: <b>{res['deleted']}</b>, выкинуто: <b>{res['kicked']}</b>.")


@dp.message(Command("risk"))
async def cmd_risk(message: Message):
    """Диагностика риск-скора: ответом на сообщение показать оценку профиля."""
    if not await _staff_only(message):
        return
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is None or target.is_bot:
        await staff_reply(message, "Ответь /risk на сообщение пользователя.")
        return
    score, reasons, dc = await risk_evaluate(target)
    v = riskscore.verdict(score, num("RISK_WATCH_THRESHOLD"), num("RISK_BAN_THRESHOLD"))
    label = {"hard": "🔴 жёсткое действие", "watch": "🟡 под наблюдение",
             "clear": "🟢 чисто"}[v]
    body = "\n".join(f"• {esc(r)}" for r in reasons) or "• сигналов нет"
    await staff_reply(message,
        f"🎯 <b>Риск-профиль</b> {mention(target)}\n"
        f"Скор: <b>{score}</b> — {label}\n"
        f"Пороги: наблюдение {num('RISK_WATCH_THRESHOLD')}, "
        f"жёстко {num('RISK_BAN_THRESHOLD')} · DC{dc if dc else '—'}\n\n"
        f"Сигналы:\n{body}")


@dp.message(Command("checkin"))
async def cmd_checkin(message: Message):
    """Проверка траста: на сколько % профиль/сообщение несёт угрозу. Ответ — в ЛС.

    /checkin ответом на сообщение (учтёт и текст на деанон/угрозу) или /checkin id|@ник.
    Видят только персонал/владелец; обычные участники — нет (см. _staff_only).
    """
    if not await _staff_only(message):
        return
    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if target is None:
        uid = _target_id(message)
        if uid is not None:
            try:
                m = await bot.get_chat_member(message.chat.id, uid)
                target = m.user
                uname_cache_add(m.user)
            except TelegramBadRequest:
                pass
    if target is None or target.is_bot:
        await staff_reply(message,
            "Ответь /checkin на сообщение пользователя или укажи id/@ник.")
        return

    score, reasons, dc = await risk_evaluate(target)
    ban_thr = num("RISK_BAN_THRESHOLD") or 85
    threat = max(0, min(100, round(score * 100 / ban_thr)))

    # /checkin ответом — текст на деанон/угрозу поднимает угрозу до 100%.
    deanon_line = ""
    text = (reply.text or reply.caption or "") if reply else ""
    if text:
        hit, why = deanon.scan_text(text, num("DEANON_MIN_HITS"))
        if hit:
            threat = 100
            deanon_line = f"\n🚫 Деанон/угроза в тексте: {esc(why)}"

    trust = 100 - threat
    mark = ("🔴 высокая угроза" if threat >= 80 else
            "🟡 подозрительно" if threat >= 40 else "🟢 чисто")
    body = "\n".join(f"• {esc(r)}" for r in reasons) or "• сигналов нет"
    await staff_reply(message,
        f"🛡 <b>Проверка траста</b> {mention(target)}\n"
        f"Угроза: <b>{threat}%</b> · траст: <b>{trust}%</b> — {mark}\n"
        f"Риск-скор: {score} (жёстко от {ban_thr}) · DC{dc if dc else '—'}"
        f"{deanon_line}\n\n"
        f"Сигналы:\n{body}")


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await _can(message, "ban"):
        return
    uid, seconds, reason = _target_dur_reason(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи id/@ник. "
                          "Можно срок и причину: /ban 3 дня спам."):
        return
    if await _deny_and_reply(message, uid):
        return
    await ban_user(message.chat.id, uid, seconds)
    rp = f" Причина: {esc(reason)}." if reason else ""
    await message.answer(f"🔨 {id_mention(uid)} забанен ({human_duration(seconds)})."
                         f"{rp} {mod_decision(message.from_user)}")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not await _can(message, "ban"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи его id/@ник."):
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await message.answer("✅ Разбанен.")
    except TelegramBadRequest as e:
        await message.answer(f"Не вышло: {e}")


@dp.message(Command("mute"))
async def cmd_mute(message: Message):
    if not await _can(message, "mute"):
        return
    uid, seconds, reason = _target_dur_reason(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи id/@ник. "
                          "Можно срок и причину: /mute 3 часа флуд."):
        return
    if await _deny_and_reply(message, uid):
        return
    await mute_user(message.chat.id, uid, seconds)
    rp = f" Причина: {esc(reason)}." if reason else ""
    await message.answer(f"🔇 {id_mention(uid)} в муте ({human_duration(seconds)})."
                         f"{rp} {mod_decision(message.from_user)}",
                         reply_markup=mod_keyboard(message.chat.id, uid))


@dp.message(Command("del", "delete"))
async def cmd_del(message: Message):
    """Тихо удалить сообщение, на которое ответили командой /del."""
    if not await _can(message, "delete"):
        return
    target = message.reply_to_message
    if not target:
        await message.answer("Ответь командой /del на сообщение, которое нужно удалить.")
        return

    deleted = False
    try:
        await target.delete()
        deleted = True
    except TelegramBadRequest as e:
        log.warning("Не смог удалить сообщение %s в чате %s: %s",
                    target.message_id, message.chat.id, e)
        await _maybe_rights_alert(message.chat.id, e)
    finally:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    if deleted:
        target_user = getattr(target, "from_user", None)
        audit(f"админ {message.from_user.full_name}", "delete message",
              getattr(target_user, "id", 0), getattr(target_user, "full_name", ""))
    else:
        await bot.send_message(message.chat.id, "Не удалось удалить сообщение: проверь права бота.")


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if not await _can(message, "mute"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи id/@ник."):
        return
    try:
        await bot.restrict_chat_member(message.chat.id, uid, permissions=FULL)
        await message.answer("✅ Размучен.")
    except TelegramBadRequest as e:
        await message.answer(f"Не вышло: {e}")


@dp.message(Command("warn"))
async def cmd_warn(message: Message):
    if not await _can(message, "warn"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь командой на сообщение нарушителя или укажи id/@ник."):
        return
    if await _deny_and_reply(message, uid):
        return
    # Имя для сообщения: из reply есть объект, иначе — ссылка по id.
    r = message.reply_to_message
    who = mention(r.from_user) if (r and r.from_user and r.from_user.id == uid) else id_mention(uid)
    n = storage.add_warn(message.chat.id, uid)
    if n >= num("WARN_LIMIT"):
        storage.reset_warns(message.chat.id, uid)
        if action_for("WARN_ACTION") == "ban":
            await ban_user(message.chat.id, uid)
            await message.answer(f"🔨 {who} забанен (лимит предупреждений).")
        else:
            await mute_user(message.chat.id, uid)
            await message.answer(f"🔇 {who} в муте (лимит предупреждений).")
    else:
        await message.answer(f"⚠️ {who}: предупреждение {n}/{num('WARN_LIMIT')}.")


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    if not await _can(message, "warn"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи id/@ник."):
        return
    storage.reset_warns(message.chat.id, uid)
    await message.answer("✅ Предупреждения сняты.")


@dp.message(Command("whitelist"))
async def cmd_whitelist(message: Message):
    if not await _staff_only(message, "words"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь командой на пользователя или укажи id/@ник, чтобы разрешить ему ссылки."):
        return
    if storage.allow_link(message.chat.id, uid):
        await message.answer("✅ Пользователю разрешены ссылки.")
    else:
        storage.disallow_link(message.chat.id, uid)
        await message.answer("🚫 Разрешение на ссылки снято.")


# Маркеры «скрытого» слова в конце команды /addword (анонимный бан).
_HIDDEN_MARKERS = {"скрыто", "скрытое", "тихо", "тайно", "hidden", "-h", "!"}


@dp.message(Command("addword"))
async def cmd_addword(message: Message):
    if not await _staff_only(message, "words"):
        return
    arg = (message.text or "").split(maxsplit=1)
    word = arg[1].strip() if len(arg) > 1 else ((message.reply_to_message.text or "").strip()
                                                 if message.reply_to_message else "")
    # Последнее слово-маркер («скрыто», «!») делает стоп-слово анонимным.
    hidden = False
    parts = word.split()
    if len(parts) > 1 and parts[-1].lower() in _HIDDEN_MARKERS:
        hidden = True
        word = " ".join(parts[:-1]).strip()
    if not word:
        await message.answer(
            "Использование: /addword слово — обычное стоп-слово.\n"
            "Можно вносить и <b>@ники</b> и <b>ссылки</b> (например @spammer или "
            "t.me/scam) — сработает на такое в тексте/имени.\n"
            "/addword слово скрыто — <b>скрытое</b>: срабатывает, но в чате не "
            "показывается (detail уходит операторам в личку).")
        return
    if storage.add_stopword(word, hidden=hidden):
        tag = " 🕵️ (скрытое)" if hidden else ""
        await message.answer(f"✅ Добавлено стоп-слово{tag}. Всего: {len(storage.stopwords())}.")
    elif hidden and storage.set_hidden_word(word, True):
        await message.answer("🕵️ Существующее стоп-слово помечено скрытым.")
    else:
        await message.answer("Такое стоп-слово уже есть.")


@dp.message(Command("delword"))
async def cmd_delword(message: Message):
    if not await _staff_only(message, "words"):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await message.answer("Использование: /delword слово")
        return
    if storage.del_stopword(arg[1].strip()):
        await message.answer(f"✅ Удалено. Осталось: {len(storage.stopwords())}.")
    else:
        await message.answer("Такого стоп-слова нет.")


@dp.message(Command("hideword"))
async def cmd_hideword(message: Message):
    """Пометить существующее стоп-слово скрытым (анонимный бан)."""
    if not await _staff_only(message, "words"):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await message.answer("Использование: /hideword слово — сделать стоп-слово скрытым.")
        return
    if storage.set_hidden_word(arg[1].strip(), True):
        await message.answer("🕵️ Слово теперь скрытое: срабатывает, но в чате не называется.")
    else:
        await message.answer("Нет такого стоп-слова (или оно уже скрытое).")


@dp.message(Command("showword"))
async def cmd_showword(message: Message):
    """Снять скрытость со стоп-слова (снова показывать в чате)."""
    if not await _staff_only(message, "words"):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await message.answer("Использование: /showword слово — снять скрытость.")
        return
    if storage.set_hidden_word(arg[1].strip(), False):
        await message.answer("👁 Слово снова открытое: причина показывается в чате.")
    else:
        await message.answer("Нет такого скрытого стоп-слова.")


@dp.message(Command("words"))
async def cmd_words(message: Message):
    if not await _staff_only(message):
        return
    words = storage.stopwords()
    if not words:
        await staff_reply(message, "Список стоп-слов пуст.")
        return
    shown = [esc(w) for w in words if not storage.is_hidden_word(w)]
    hidden = [esc(w) for w in words if storage.is_hidden_word(w)]
    out = []
    if shown:
        out.append("📋 <b>Открытые</b> (причина видна в чате):\n" + ", ".join(shown))
    if hidden:
        out.append("🕵️ <b>Скрытые</b> (в чате не называются):\n" + ", ".join(hidden))
    await staff_reply(message, "\n\n".join(out))


@dp.message(Command("trust"))
async def cmd_trust(message: Message):
    if not await _staff_only(message, "manage"):
        return
    uid = _target_id(message)
    if await _need_target(message, uid,
                          "Ответь /trust на пользователя или укажи id/@ник (он будет мимо всех проверок)."):
        return
    added = storage.toggle_trusted(message.chat.id, uid)
    await message.answer("✅ Добавлен в доверенные." if added else "➖ Убран из доверенных.")


@dp.message(Command("setrole"))
async def cmd_setrole(message: Message):
    """Выдать внутреннюю роль. Раздавать роли может ТОЛЬКО владелец (см. панель → 👑 Владельцы).

    Формы: ответом «/setrole роль», либо «/setrole <id|@ник> роль».
    """
    if not (message.from_user and storage.is_owner(message.from_user.id)):
        return
    parts = (message.text or "").split()
    r = message.reply_to_message
    mid = _mention_uid(message)
    if r and r.from_user:                              # ответом: роль — первый аргумент
        uname_cache_add(r.from_user)
        uid, raw = r.from_user.id, (parts[1] if len(parts) > 1 else "")
    elif mid is not None:                              # tap-упоминание: роль — первый аргумент
        uid, raw = mid, (parts[1] if len(parts) > 1 else "")
    elif len(parts) > 2 and _is_target_token(parts[1]):  # «/setrole <id|@ник> роль»
        uid, raw = _resolve_target_token(parts[1]), parts[2]
        if uid is None:
            await message.answer(_UNKNOWN_UNAME)
            return
    else:
        await message.answer("Использование: /setrole роль (ответом) или /setrole id роль.\n"
                             f"Роли: {_roles_hint()}")
        return
    role = resolve_role(raw)
    if role is None:
        await message.answer(f"Нет роли «{esc(raw)}». Доступны: {_roles_hint()}")
        return
    storage.set_role(uid, role)
    await message.answer(f"✅ {id_mention(uid, await display_name(message.chat.id, uid))} — "
                         f"роль {role_label(role)} (ранг {role_rank(role)}, "
                         f"права: {', '.join(sorted(role_perms(role))) or '—'}).")


@dp.message(Command("delrole"))
async def cmd_delrole(message: Message):
    if not (message.from_user and storage.is_owner(message.from_user.id)):
        return
    uid = _target_id(message)
    if await _need_target(message, uid, "Ответь /delrole на пользователя или укажи id/@ник."):
        return
    storage.set_role(uid, None)
    await message.answer("✅ Роль снята.")


@dp.message(Command("roles"))
async def cmd_roles(message: Message):
    if not (message.from_user and storage.is_owner(message.from_user.id)):
        return
    lines = ["🎖 <b>Роли</b> (Telegram-админы имеют все права по умолчанию)\n",
             "Иерархия (от старшей к младшей):"]
    # Роли по убыванию ранга — наглядная иерархия.
    for name in sorted(config.ROLES, key=lambda n: role_rank(n), reverse=True):
        lines.append(f"• {role_label(name)} — ранг {role_rank(name)}: "
                     f"{', '.join(sorted(role_perms(name)))}")
    rr = storage.roles_all()
    if rr:
        lines.append("\nНазначено:")
        # Сначала старшие по должности.
        ordered = sorted(rr.items(), key=lambda kv: role_rank(kv[1]), reverse=True)
        for u, role in ordered:
            try:
                who = await display_name(message.chat.id, int(u))
            except (ValueError, TypeError):
                who = f"id{u}"
            lines.append(f"• {role_label(role)} — {id_mention(int(u), who)} "
                         f"(<code>{u}</code>)")
    else:
        lines.append("\nПока никому. Выдать: /setrole роль (ответом) "
                     "или /setrole @ник роль.")
    await message.answer("\n".join(lines))


@dp.message(Command("renamerole"))
async def cmd_renamerole(message: Message):
    """Переименовать ранг (только владелец). /renamerole <роль> <новое имя>; «-» — сброс.

    Меняется только ОТОБРАЖАЕМОЕ имя; ключ роли и её права/ранг остаются прежними,
    так что /setrole понимает и старый ключ, и новое название.
    """
    if not (message.from_user and storage.is_owner(message.from_user.id)):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /renamerole <роль> <новое название>\n"
                             f"Роли: {_roles_hint()}\n"
                             "Сброс к исходному: /renamerole <роль> -")
        return
    role = resolve_role(parts[1])
    if role is None:
        await message.answer(f"Нет роли «{esc(parts[1])}». Доступны: {_roles_hint()}")
        return
    new = parts[2].strip()
    if new == "-":
        storage.set_role_title(role, None)
        await message.answer(f"✅ Название ранга сброшено: {role_label(role)}")
    else:
        storage.set_role_title(role, new)
        await message.answer(f"✅ Ранг «{esc(role)}» теперь отображается как {role_label(role)}")


@dp.message(Command("rules"))
async def cmd_rules(message: Message):
    rules = storage.get_rules()
    await message.answer(render_rules(rules) if rules else "📜 Правила пока не заданы.",
                         disable_web_page_preview=True)


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(storage.get_str("PRIVACY_TEXT", config.PRIVACY_TEXT),
                         disable_web_page_preview=True)


@dp.message(Command("setrules"))
async def cmd_setrules(message: Message):
    if not await _staff_only(message, "manage"):
        return
    # Восстанавливаем ссылки из entity — иначе часть [текст](url) теряется.
    if len(message.text.split(maxsplit=1)) > 1:
        txt = _md_after_command(message)
    elif message.reply_to_message:
        txt = message_markdown(message.reply_to_message).strip()
    else:
        txt = ""
    if not txt:
        await message.answer(
            "Использование: /setrules текст (или ответом на сообщение).\n"
            "Ссылку вшивай так: <code>[наш канал](https://t.me/...)</code> — "
            "станет кликабельной.")
        return
    storage.set_rules(txt)
    await message.answer("✅ Правила сохранены. Проверь: /rules", disable_web_page_preview=True)


@dp.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message):
    if not await _staff_only(message, "manage"):
        return
    # Восстанавливаем ссылки из entity — иначе часть [текст](url) теряется.
    if len(message.text.split(maxsplit=1)) > 1:
        txt = _md_after_command(message)
    elif message.reply_to_message:
        txt = message_markdown(message.reply_to_message).strip()
    else:
        txt = ""
    if not txt:
        await message.answer("Использование: /setwelcome текст. Ссылки: [текст](https://...).")
        return
    storage.set_str("WELCOME_TEXT", txt)
    storage.set_flag("WELCOME_ENABLED", True)
    await message.answer("✅ Приветствие сохранено и включено.", disable_web_page_preview=True)


@dp.message(Command("addtrigger"))
async def cmd_addtrigger(message: Message):
    if not await _staff_only(message, "manage"):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or "|" not in arg[1]:
        await message.answer("Использование: /addtrigger ключ | ответ\n"
                             "Пример: /addtrigger правила | Читай [тут](https://t.me/...)")
        return
    key, reply = arg[1].split("|", 1)
    key, reply = key.strip(), reply.strip()
    if not key or not reply:
        await message.answer("Нужны и ключ, и ответ через | .")
        return
    storage.add_trigger(key, reply)
    await message.answer(f"✅ Автоответ на «{esc(key)}» сохранён ({len(storage.triggers())} всего).")


@dp.message(Command("deltrigger"))
async def cmd_deltrigger(message: Message):
    if not await _staff_only(message, "manage"):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await message.answer("Использование: /deltrigger ключ")
        return
    if storage.del_trigger(arg[1]):
        await message.answer("✅ Удалено.")
    else:
        await message.answer("Такого триггера нет.")


@dp.message(Command("triggers"))
async def cmd_triggers(message: Message):
    if not await _staff_only(message):
        return
    trg = storage.triggers()
    if not trg:
        await message.answer("Автоответов нет. Добавить: /addtrigger ключ | ответ")
    else:
        await message.answer("🔔 Автоответы:\n" + "\n".join(f"• {esc(k)}" for k in trg))


async def _ack(chat_id: int, text: str, seconds: int = 5):
    m = await bot.send_message(chat_id, text)
    asyncio.create_task(_autodelete(chat_id, m.message_id, seconds))


@dp.message(Command("report"))
async def cmd_report(message: Message):
    if not flag("REPORT_ENABLED"):
        return
    r = message.reply_to_message
    if not r or not r.from_user:
        await message.answer("Ответь /report на сообщение нарушителя.")
        return
    chat_id = message.chat.id
    reporter = message.from_user
    target = r.from_user

    # --- анти-абуз ---
    if target.id == reporter.id:
        await _ack(chat_id, "На себя жаловаться нельзя 🙂")
        return
    if await is_admin(chat_id, target.id) or storage.is_trusted(chat_id, target.id):
        await _ack(chat_id, "На админа/доверенного жалоба отклонена.")
        return
    last = report_cooldown.get((chat_id, reporter.id))
    if last and (now() - last).total_seconds() < config.REPORT_COOLDOWN:
        return
    tbuf = report_times.setdefault((chat_id, reporter.id), deque(maxlen=50))
    tbuf.append(now())
    hcut = now() - timedelta(hours=1)
    while tbuf and tbuf[0] < hcut:
        tbuf.popleft()
    if len(tbuf) > config.REPORT_MAX_PER_HOUR:
        await _ack(chat_id, "Слишком много жалоб. Притормози.")
        return
    report_cooldown[(chat_id, reporter.id)] = now()

    # --- голосование по конкретному сообщению ---
    vkey = (chat_id, r.message_id)
    entry = report_votes.setdefault(vkey, {"voters": set(), "ts": now(), "done": False})
    entry["voters"].add(reporter.id)
    votes = len(entry["voters"])
    stats["reports"] += 1

    card = event_card("⚠️ Жалоба на пользователя", target,
                      text=(r.text or r.caption or ""), when=r.date)
    card += f"\nЖалуется: {mention(reporter)}\nГолосов: <b>{votes}/{config.REPORT_VOTES}</b>"

    # уведомляем админов только на первый голос (без спама) и на авто-действие
    if votes == 1 and flag("NOTIFY_REPORTS"):
        await notify_report(r, reporter, card)
    if config.LOG_CHAT_ID and votes == 1:
        link = message_link(chat_id, r.message_id)
        rows = ([[InlineKeyboardButton(text="🔗 Перейти к сообщению", url=link)]] if link else [])
        rows += mod_rows(chat_id, target.id)
        await report(chat_id, card, InlineKeyboardMarkup(inline_keyboard=rows))

    # авто-действие по набору голосов
    if votes >= config.REPORT_VOTES and not entry["done"]:
        entry["done"] = True
        act = config.REPORT_AUTO_ACTION
        if act == "ban":
            await ban_user(chat_id, target.id)
        elif act == "mute":
            await mute_user(chat_id, target.id)
        try:
            await bot.delete_message(chat_id, r.message_id)
        except TelegramBadRequest:
            pass
        audit("голосование", f"{act} ({votes} жалоб)", target.id, target.full_name)
        await report(chat_id, f"⚖️ {mention(target)} — авто-{act} по {votes} жалобам.")
        await notify_panel(f"⚖️ Авто-{act}: {mention(target)} набрал {votes} жалоб.")

    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await _ack(chat_id, "✅ Жалоба учтена.", 40)


# Тайм-наказания текстом в чате: «мут 3 дня», «бан 2 часа» (ответом на юзера).
# Жёсткий шаблон: ВСЁ сообщение = слово + необяз. срок, иначе не срабатывает
# (поэтому «я тебе сейчас мут дам» НЕ триггерит).
NL_PATTERN = (r"(?i)^\s*(мут|размут|бан|разбан|варн|кик|mute|unmute|ban|unban|warn|kick)"
              r"(?:\s+\d+\s*[а-яёa-z.]*)?\s*$")


_WORD_PERM = {"мут": "mute", "mute": "mute", "размут": "mute", "unmute": "mute",
              "бан": "ban", "ban": "ban", "разбан": "ban", "unban": "ban",
              "варн": "warn", "warn": "warn", "кик": "kick", "kick": "kick"}


# ======================= Система репутации (+реп / -реп) ====================
# Участники в ответ на сообщение пишут «+реп»/«-реп» (или /rep) и меняют
# репутацию автора на ±1. На одного получателя — не чаще раза в REP_COOLDOWN_HOURS
# (по умолчанию сутки). Себе и ботам нельзя. Визуал: ранг-бейдж, шкала 👍/👎, /toprep.

# «+реп», «-реп», «+rep», «-rep», «+респект» и т.п. в начале строки.
REP_PATTERN = r"(?i)^\s*([+\-])\s*(?:реп|rep|rp|респект|репутаци\w*|respect)\b"

# Ранги по очкам: (порог_минимум, эмодзи, название). Берём последний подходящий.
REP_TIERS = [
    (-10 ** 9, "☠️", "Изгой"),
    (-4,       "⚠️", "Подозрительный"),
    (0,        "🌱", "Новичок"),
    (10,       "🙂", "Участник"),
    (25,       "⭐", "Уважаемый"),
    (50,       "🏅", "Ветеран"),
    (100,      "💎", "Авторитет"),
    (250,      "👑", "Легенда"),
]


def rep_badge(score: int) -> tuple[str, str]:
    """(эмодзи, название ранга) по числу очков репутации."""
    badge, title = "🌱", "Новичок"
    for threshold, emoji, name in REP_TIERS:
        if score >= threshold:
            badge, title = emoji, name
    return badge, title


def rep_bar(rec: dict, width: int = 10) -> str:
    """Визуальная шкала 👍/👎 из зелёных/красных квадратов."""
    plus, minus = rec.get("plus", 0), rec.get("minus", 0)
    total = plus + minus
    if not total:
        return "▫️" * width
    filled = round(plus / total * width)
    filled = max(0, min(width, filled))
    return "🟩" * filled + "🟥" * (width - filled)


def rep_card(name: str, rec: dict) -> str:
    """Карточка репутации пользователя (для /rep и начисления)."""
    badge, title = rep_badge(rec["score"])
    return (f"{badge} <b>Репутация</b> — {esc(name)}\n"
            f"Ранг: <b>{title}</b>\n"
            f"Очки: <b>{rec['score']:+d}</b>\n"
            f"{rep_bar(rec)}\n"
            f"👍 {rec['plus']}   👎 {rec['minus']}")


async def _rep_notice(message: Message, text: str, ttl: int = 12) -> None:
    """Короткое уведомление, которое само удалится (чтобы не сорить в чате)."""
    try:
        m = await message.reply(text, disable_web_page_preview=True)
        asyncio.create_task(_autodelete(message.chat.id, m.message_id, ttl))
    except TelegramBadRequest:
        pass


@dp.message(F.reply_to_message, F.text.regexp(REP_PATTERN),
            F.chat.type.in_({"group", "supergroup"}))
async def on_rep(message: Message):
    if not flag("REP_ENABLED"):
        return
    giver = message.from_user
    target = message.reply_to_message.from_user
    if not giver or not target:
        return
    chat_id = message.chat.id
    m = re.match(REP_PATTERN, message.text or "")
    if not m:
        return
    delta = 1 if m.group(1) == "+" else -1
    if target.id == giver.id:
        await _rep_notice(message, "🚫 Нельзя менять репутацию самому себе.")
        return
    if target.is_bot:
        await _rep_notice(message, "🤖 Ботам репутация не начисляется.")
        return
    # Кулдаун: одному человеку — раз в REP_COOLDOWN_HOURS.
    cd = num("REP_COOLDOWN_HOURS") * 3600
    last_dt = _parse_ts(storage.rep_last_given(chat_id, giver.id, target.id))
    if cd > 0 and last_dt:
        elapsed = (now() - last_dt).total_seconds()
        if elapsed < cd:
            left = int(cd - elapsed)
            await _rep_notice(
                message,
                f"⏳ Ты уже оценивал(а) этого участника. "
                f"Повторно можно через {human_duration(left)}.")
            return
    uname_cache_add(target)
    uname_cache_add(giver)
    rec = storage.add_rep(chat_id, giver.id, target.id, delta, now().isoformat())
    if flag("REP_ANNOUNCE"):
        verb = "повысил репутацию" if delta > 0 else "понизил репутацию"
        sign = "➕" if delta > 0 else "➖"
        try:
            await message.reply(
                f"{sign} {mention(giver)} {verb} → {mention(target)}\n\n"
                f"{rep_card(target.full_name, rec)}",
                disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
    else:
        badge, _ = rep_badge(rec["score"])
        await _rep_notice(
            message,
            f"{'➕' if delta > 0 else '➖'} Репутация {mention(target)}: "
            f"<b>{rec['score']:+d}</b> {badge}", ttl=8)


@dp.message(Command("rep", "reputation", "myrep"),
            F.chat.type.in_({"group", "supergroup"}))
async def cmd_rep(message: Message):
    """Показать карточку репутации (ответом на юзера, по id/@нику или свою)."""
    if not flag("REP_ENABLED"):
        return
    chat_id = message.chat.id
    uid = _target_id(message)
    if not uid:
        uid = message.from_user.id if message.from_user else None
    if not uid:
        await message.answer("Ответь /rep на пользователя или напиши /rep для своей репутации.")
        return
    name = await display_name(chat_id, uid)
    await message.answer(rep_card(name, storage.get_rep(chat_id, uid)),
                         disable_web_page_preview=True)


@dp.message(Command("toprep", "reptop", "topreputation"),
            F.chat.type.in_({"group", "supergroup"}))
async def cmd_toprep(message: Message):
    """Топ участников по репутации в этом чате."""
    if not flag("REP_ENABLED"):
        return
    chat_id = message.chat.id
    top = storage.rep_top(chat_id, 10)
    if not top:
        await message.answer("📊 Репутаций пока нет. Отвечай «+реп» / «-реп» на сообщения участников.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ по репутации</b>"]
    for i, (uid, rec) in enumerate(top):
        badge, _ = rep_badge(rec["score"])
        pos = medals[i] if i < 3 else f"{i + 1}."
        name = await display_name(chat_id, uid)
        lines.append(f"{pos} {badge} {esc(name)} — <b>{rec['score']:+d}</b> "
                     f"(👍{rec['plus']}/👎{rec['minus']})")
    await message.answer("\n".join(lines))


@dp.message(F.reply_to_message, F.text.regexp(NL_PATTERN))
async def nl_command(message: Message):
    if not message.from_user:
        return
    target = message.reply_to_message.from_user
    if not target:
        return
    chat_id, uid = message.chat.id, target.id
    text = message.text.strip().lower()
    word = re.match(r"^\s*([а-яёa-z]+)", text).group(1)
    if not await can(chat_id, message.from_user.id, _WORD_PERM.get(word, "ban")):
        return
    # Прячем запрос до проверки цели, чтобы даже неудачное действие
    # администратора не оставалось в общем чате.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    # Само-защита/иерархия — только для карающих слов (размут/разбан пропускаем).
    if word in ("мут", "mute", "бан", "ban", "варн", "warn", "кик", "kick"):
        reason = await _deny_target(chat_id, message.from_user.id, uid)
        if reason:
            await message.answer(reason)
            return
    seconds = parse_duration(text)
    by = f" {mod_decision(message.from_user)}"
    audit(f"админ {message.from_user.full_name}", f"{word} {human_duration(seconds)}",
          uid, target.full_name)
    if word in ("мут", "mute"):
        await mute_user(chat_id, uid, seconds)
        await report(chat_id, f"🔇 {mention(target)} в муте ({human_duration(seconds)}).{by}",
                     mod_keyboard(chat_id, uid))
    elif word in ("размут", "unmute"):
        try:
            await bot.restrict_chat_member(chat_id, uid, permissions=FULL)
        except TelegramBadRequest:
            pass
        await report(chat_id, f"✅ {mention(target)} размучен.{by}")
    elif word in ("бан", "ban"):
        await ban_user(chat_id, uid, seconds)
        await report(chat_id, f"🔨 {mention(target)} забанен ({human_duration(seconds)}).{by}")
    elif word in ("разбан", "unban"):
        try:
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        except TelegramBadRequest:
            pass
        await report(chat_id, f"✅ {mention(target)} разбанен.{by}")
    elif word in ("варн", "warn"):
        n = storage.add_warn(chat_id, uid)
        if n >= num("WARN_LIMIT"):
            storage.reset_warns(chat_id, uid)
            await (ban_user if action_for("WARN_ACTION") == "ban" else mute_user)(chat_id, uid)
            await report(chat_id, f"🔨 {mention(target)} — лимит предупреждений.{by}")
        else:
            await report(chat_id, f"⚠️ {mention(target)}: предупреждение {n}/{num('WARN_LIMIT')}.{by}")
    elif word in ("кик", "kick"):
        await bot.ban_chat_member(chat_id, uid)
        try:
            await bot.unban_chat_member(chat_id, uid)
        except TelegramBadRequest:
            pass
        await report(chat_id, f"👢 {mention(target)} кикнут.{by}")


def _parse_onoff(message: Message) -> bool | None:
    parts = (message.text or "").lower().split()
    if len(parts) > 1:
        if parts[1] in ("on", "вкл", "1", "да"):
            return True
        if parts[1] in ("off", "выкл", "0", "нет"):
            return False
    return None


@dp.message(Command("night"))
async def cmd_night(message: Message):
    if not await _staff_only(message, "manage"):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"Ночной режим: {'вкл' if flag('NIGHT_MODE') else 'выкл'}. "
                            f"Используй /night on|off. Часы: {config.NIGHT_START}–{config.NIGHT_END}.")
        return
    storage.set_flag("NIGHT_MODE", v)
    await message.answer(f"🌙 Ночной режим: {'включён' if v else 'выключен'}.")


@dp.message(Command("quiet"))
async def cmd_quiet(message: Message):
    if not await _staff_only(message, "manage"):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"Тихий режим: {'вкл' if flag('QUIET_MODE') else 'выкл'}. /quiet on|off")
        return
    storage.set_flag("QUIET_MODE", v)
    await message.answer(f"🤫 Тихий режим: {'включён' if v else 'выключен'}.")


@dp.message(Command("antimat"))
async def cmd_antimat(message: Message):
    if not await _staff_only(message, "manage"):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"Антимат: {'вкл' if flag('ANTIMAT_ENABLED') else 'выкл'}. /antimat on|off")
        return
    storage.set_flag("ANTIMAT_ENABLED", v)
    await message.answer(f"🤬 Антимат: {'включён' if v else 'выключен'}.")


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not await _staff_only(message):
        return
    def s(name):
        return "вкл" if flag(name) else "выкл"
    await message.answer(
        "⚙️ <b>Настройки</b>\n"
        f"Антимат: {s('ANTIMAT_ENABLED')} | Ссылки-блок: {s('BLOCK_LINKS')} "
        f"(упоминания: {s('ALLOW_MENTIONS')})\n"
        f"Пересылки-блок: {s('BLOCK_FORWARDS')} | Каналы-блок: {s('BLOCK_CHANNEL_MESSAGES')}\n"
        f".apk-блок: {s('BLOCK_APK')} | Премиум-эмодзи-блок: {s('BLOCK_PREMIUM_EMOJI')}\n"
        f"Антифлуд: {s('ANTIFLOOD_ENABLED')} ({config.ANTIFLOOD_COUNT}/{config.ANTIFLOOD_SECONDS}с)\n"
        f"Ночной режим: {s('NIGHT_MODE')} ({config.NIGHT_START}–{config.NIGHT_END}) | "
        f"Тихий: {s('QUIET_MODE')}\n"
        f"Проверка имён: {s('CHECK_JOIN_NAMES')} | Приветствие: {s('WELCOME_ENABLED')}\n"
        f"Стоп-слов: {len(storage.stopwords())} | Эталонов: {len(ref_hashes)} | "
        f"NSFW 18+ (ViT): {'вкл' if nsfwvit.available() else 'выкл'}"
    )


def stats_text() -> str:
    return (
        "📊 <b>Статистика</b>\n"
        f"Выдано капч: {stats['challenged']} | прошли: {stats['passed']} | "
        f"завалили: {stats['failed']}\n"
        f"Мутов за картинки: {stats['img_muted']} | банов всего: {stats['banned']}\n"
        f"Жалоб: {stats.get('reports', 0)} | рейдов: {stats.get('raids', 0)} | "
        f"ЧС: {stats.get('crises', 0)}\n"
        f"Риск-мутов: {stats.get('risk_muted', 0)} | кикнуто удалёнок: {stats.get('deleted_kicked', 0)}\n"
        f"Деанон/угрозы (текст): {stats.get('deanon_text', 0)}\n"
        f"Под наблюдением: {sum(1 for v in probation.values() if v['until'] >= now())}\n"
        f"Эталонов: {len(ref_hashes)} | стоп-слов: {len(storage.stopwords())}\n"
        f"Сейчас на капче: {sum(1 for v in pending.values() if v.get('steps'))}"
    )


def audit_text(n: int = 15, *, include_actor: bool = True) -> str:
    items = storage.get_audit(n)
    if not items:
        return "📒 Журнал действий пуст."
    lines = ["📒 <b>Журнал действий</b> (свежие сверху)"]
    for e in items:
        who = e.get("target_name") or e.get("target_id")
        actor = f"{esc(e['actor'])} → " if include_actor else ""
        line = f"{e['ts']} · {actor}<b>{esc(e['action'])}</b> · {esc(str(who))}"
        if e.get("reason"):
            line += f" ({esc(e['reason'])})"
        lines.append(line)
    return "\n".join(lines)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _staff_only(message):
        return
    await message.answer(stats_text())


@dp.message(Command("log"))
async def cmd_log(message: Message):
    if not await _staff_only(message):
        return
    await staff_reply(message, audit_text(20, include_actor=True))


@dp.message(Command("info"))
async def cmd_info(message: Message):
    if not await _staff_only(message):
        return
    uid = _target_id(message)
    if await _need_target(message, uid, "Ответь /info на пользователя или укажи id/@ник."):
        return
    chat_id = message.chat.id
    name, uname, status = str(uid), "—", "?"
    try:
        m = await bot.get_chat_member(chat_id, uid)
        name = m.user.full_name
        uname = f"@{m.user.username}" if m.user.username else "—"
        status = str(m.status)
        uname_cache_add(m.user)
    except TelegramBadRequest:
        pass
    # Внутренняя роль/должность и её место в иерархии.
    role = storage.get_role(uid)
    if await is_admin(chat_id, uid):
        role_line = "Роль: 🛡 TG-админ чата (все права)"
    elif role:
        override = storage.get_user_perms_override(uid) is not None
        src = " ✏️ личный набор" if override else ""
        role_line = (f"Роль: {role_label(role)} (ранг {role_rank(role)}, "
                     f"права: {', '.join(sorted(effective_perms(uid))) or '—'}){src}")
    else:
        role_line = "Роль: — (обычный участник)"
    if storage.is_owner(uid):
        role_line += " 👑 владелец бота"
    joined = newcomer.get((chat_id, uid))
    txt = (
        "👤 <b>Досье</b>\n"
        f"ID: <code>{uid}</code>\nИмя: {esc(name)}\nЮзер: {esc(uname)}\n"
        f"Статус в чате: {esc(status)}\n"
        f"{role_line}\n"
        f"Предупреждений: {storage.get_warns(chat_id, uid)}/{num('WARN_LIMIT')}\n"
        f"Доверенный: {'да' if storage.is_trusted(chat_id, uid) else 'нет'} | "
        f"ссылки: {'разрешены' if storage.link_allowed(chat_id, uid) else 'нет'}\n"
        f"Сообщений (с запуска бота): {msgcount.get((chat_id, uid), 0)}"
    )
    act = storage.get_activity(chat_id, uid)
    if act:
        _first = _parse_ts(act.get("first_seen"))
        _reg = "старожил (мягкий режим)" if is_regular(chat_id, uid) else "обычный"
        _amsgs = act.get("msgs", 0)
        txt += chr(10) + f"Активность: {_amsgs} сообщений всего | статус: {_reg}"
        if _first:
            txt += chr(10) + f"Первое сообщение: {fmt_when(_first)}"
    if joined:
        txt += f"\nВошёл: {fmt_when(joined)}"
    await message.answer(txt)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    """История наказаний/действий по пользователю (из журнала). /history (ответом|id|@ник)."""
    if not await _staff_only(message):
        return
    uid = _target_id(message)
    if await _need_target(message, uid, "Ответь /history на пользователя или укажи id/@ник."):
        return
    items = storage.get_audit_for(uid, 20)
    if not items:
        await message.answer(f"📭 По <code>{uid}</code> в журнале пусто "
                             f"(хранятся последние {storage.AUDIT_LIMIT} записей).")
        return
    name = items[0].get("target_name") or str(uid)
    lines = [f"🗂 <b>История</b> — {esc(str(name))} (<code>{uid}</code>), свежие сверху:"]
    public_anon = (message.chat.type in ("group", "supergroup") and flag("ANON_ADMIN"))
    for e in items:
        actor = f"{esc(e['actor'])} → " if not public_anon else ""
        line = f"{e['ts']} · {actor}<b>{esc(e['action'])}</b>"
        if e.get("reason"):
            line += f" ({esc(e['reason'])})"
        lines.append(line)
    await message.answer("\n".join(lines))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    u = message.from_user
    is_staff = bool(u and (await is_admin(message.chat.id, u.id) or storage.get_role(u.id)))
    if not is_staff:
        # Обычным участникам — короткая справка по доступным им командам.
        await message.answer(
            "🤝 <b>Команды участника</b>\n"
            "/rules — правила чата\n"
            "/report (ответом на сообщение) — пожаловаться модераторам\n"
            "/vb (ответом) — предложить голосование за заглушение\n"
            "⭐ <b>Репутация:</b> ответь «+реп» / «-реп» на сообщение (раз в сутки)\n"
            "/rep (ответом или свою) | /toprep — топ чата\n"
            "/ping — проверить, что бот жив"
        )
        return
    await message.answer(
        "🛡 <b>Команды админов</b>\n"
        "\n"
        "🔨 <b>Модерация</b>\n"
        "/del — тихо удалить сообщение ответом\n"
        "/ban /unban /mute /unmute — ответом, по id или @нику\n"
        "  (срок и причина: <code>/mute @ivan 3 дня флуд</code>)\n"
        "/warn /unwarn — предупреждения\n"
        "/whitelist — разрешить ссылку | /trust — доверенный\n"
        "Текстом ответом: <code>мут 3 часа</code>, <code>бан 2 дня</code>, "
        "<code>размут</code>, <code>варн</code>, <code>кик</code>\n"
        "\n"
        "🎖 <b>Роли</b> (👑админ &gt; ⭐старший &gt; 🎖модератор)\n"
        "/setrole [@ник|id] роль | /delrole | /roles\n"
        "/renamerole &lt;роль&gt; &lt;новое имя&gt;\n"
        "\n"
        "🧱 <b>Фильтры и контент</b>\n"
        "/spam (ответом на картинку) | /reload — база спама\n"
        "/allowpack /denypack /stickers — стикерпаки (не 18+)\n"
        "/addword /delword /words — стоп-слова\n"
        "  (скрытые — не видны в чате: <code>/addword слово скрыто</code>, "
        "/hideword, /showword)\n"
        "  (ловят похожие: «спааам», «с-п-а-м» — тумблер «Похожие слова» в /admin)\n"
        "\n"
        "💬 <b>Чат</b>\n"
        "/rules /setrules — правила | /setwelcome — приветствие\n"
        "/addtrigger ключ | ответ, /deltrigger, /triggers — автоответы\n"
        "/night /quiet /antimat on|off — режимы\n"
        "\n"
        "🚨 <b>Рейд и заявки</b>\n"
        "/crisis on|off — автономный ЧС-режим (сам ловит рейд и откатывает)\n"
        "/lockdown on|off | /checkdc | /purgedc\n"
        "/clearrequests — отклонить заявки\n"
        "\n"
        "🎯 <b>Профили и чистка</b>\n"
        "/risk (ответом) — оценка | /cleandeleted — удалёнки\n"
        "/scanall — полный скан (юзербот)\n"
        "\n"
        "📊 <b>Инфо</b>\n"
        "/info (досье) | /history (наказания) | /log | /diag | /settings | /stats | /ping\n"
        "/check (ответом на фото) | /vb (голосование) | /report\n"
        "\n"
        "⚙️ Всё это удобнее в личке бота — команда /admin (пароль)."
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("pong ✅ бот живой")


# ------------------------------------------------------------- игра «кубик»
def _dice_leaders(rolls: dict) -> list:
    """uid с максимальным броском: один — победитель, несколько — переброс."""
    if not rolls:
        return []
    top = max(rolls.values())
    return [u for u, v in rolls.items() if v == top]


def _dice_board(game: dict) -> str:
    ps = game["players"]
    who = "\n".join("• " + id_mention(u, n) for u, n in ps.items()) or "<i>пока никто</i>"
    return ("🎲 <b>Игра в кубик</b>\n"
            "Игроков: <b>%d</b>\n%s\n\n"
            "Жми «🎲 Играть», затем любой игрок — «▶️ Бросать». У кого больше — тот выиграл."
            % (len(ps), who))


def _dice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 Играть", callback_data="dg:join"),
        InlineKeyboardButton(text="▶️ Бросать", callback_data="dg:go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="dg:cancel"),
    ]])


@dp.message(Command("kubik", "dice", "game"), F.chat.type.in_({"group", "supergroup"}))
async def dice_start(message: Message):
    chat_id = message.chat.id
    if chat_id in dice_games:
        await message.reply("🎲 Игра уже идёт — жми кнопки под сообщением игры.")
        return
    game = {"players": {}, "host": message.from_user.id, "rolling": False, "msg_id": 0}
    dice_games[chat_id] = game
    sent = await message.answer(_dice_board(game), reply_markup=_dice_kb())
    game["msg_id"] = sent.message_id
    asyncio.create_task(_dice_expire(chat_id, game))


@dp.callback_query(F.data.startswith("dg:"))
async def dice_cb(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    game = dice_games.get(chat_id)
    if not game:
        await cb.answer("Игра уже завершена.")
        return
    action = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    if action == "join":
        if game["rolling"]:
            await cb.answer("Уже бросаем — дождись конца.")
            return
        game["players"][uid] = cb.from_user.full_name
        await cb.answer("Ты в игре! 🎲")
        try:
            await cb.message.edit_text(_dice_board(game), reply_markup=_dice_kb())
        except TelegramBadRequest:
            pass
    elif action == "cancel":
        # Отменять может кто угодно — чтобы «зависшую» игру всегда можно закрыть.
        dice_games.pop(chat_id, None)
        await cb.answer("Отменено.")
        try:
            await cb.message.edit_text("🎲 Игра отменена.")
        except TelegramBadRequest:
            pass
        asyncio.create_task(_dice_cleanup(chat_id, [game["msg_id"]]))
    elif action == "go":
        # Запустить бросок может любой игрок (не только основатель).
        if len(game["players"]) < 2:
            await cb.answer("Нужно минимум 2 игрока.", show_alert=True)
            return
        if game["rolling"]:
            await cb.answer()
            return
        game["rolling"] = True
        await cb.answer("Бросаем! 🎲")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _dice_run(chat_id, game)
    else:
        await cb.answer()


async def _dice_cleanup(chat_id: int, msg_ids: list, delay: int = 300):
    """Удалить сообщения игры через delay секунд после её конца (по умолч. 5 мин)."""
    await asyncio.sleep(delay)
    ids = [m for m in msg_ids if m]
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(chat_id, ids[i:i + 100])
        except TelegramBadRequest:
            pass


async def _dice_expire(chat_id: int, game: dict, delay: int = 600):
    """Авто-снять зависшую игру: если за delay сек. так и не начали бросать —
    убрать её, чтобы /kubik снова работал. Иначе игру уже не начать заново."""
    await asyncio.sleep(delay)
    if dice_games.get(chat_id) is game and not game["rolling"]:
        dice_games.pop(chat_id, None)
        try:
            await bot.edit_message_text("🎲 Игра истекла — начните заново: /kubik",
                                        chat_id, game["msg_id"])
        except TelegramBadRequest:
            pass
        asyncio.create_task(_dice_cleanup(chat_id, [game["msg_id"]]))


async def _dice_run(chat_id: int, game: dict):
    players = dict(game["players"])
    contenders = dict(players)
    msgs = [game.get("msg_id")]
    try:
        while True:
            rolls = {}
            for u in list(contenders):
                m = await bot.send_dice(chat_id)
                msgs.append(m.message_id)
                rolls[u] = m.dice.value
                await asyncio.sleep(0.4)
            await asyncio.sleep(3.5)
            summary = ", ".join("%s — <b>%d</b>" % (esc(contenders[u]), rolls[u])
                                for u in contenders)
            leaders = _dice_leaders(rolls)
            if len(leaders) == 1:
                win = leaders[0]
                r = await bot.send_message(
                    chat_id,
                    "🏆 Победитель: %s (выпало <b>%d</b>)\n%s"
                    % (id_mention(win, players[win]), rolls[win], summary))
                msgs.append(r.message_id)
                return
            tied = ", ".join(esc(contenders[u]) for u in leaders)
            r = await bot.send_message(
                chat_id,
                "🤝 Ничья на <b>%d</b>: %s — перебрасывают!\n%s"
                % (max(rolls.values()), tied, summary))
            msgs.append(r.message_id)
            contenders = {u: players[u] for u in leaders}
    finally:
        dice_games.pop(chat_id, None)
        asyncio.create_task(_dice_cleanup(chat_id, msgs))


@dp.message(F.text, F.chat.type.in_({"group", "supergroup"}))
async def on_trigger(message: Message):
    """Автоответы на ключевые слова. Регистрируется ПОСЛЕ всех команд."""
    if not flag("TRIGGERS_ENABLED"):
        return
    trg = storage.triggers()
    if not trg:
        return
    low = textguard.normalize(message.text)
    for key, reply in trg.items():
        if key in low:
            last = trigger_cooldown.get(message.chat.id)
            if last and (now() - last).total_seconds() < 5:
                return
            trigger_cooldown[message.chat.id] = now()
            try:
                await message.reply(render_rules(reply), disable_web_page_preview=True)
            except TelegramBadRequest:
                pass
            return


@dp.edited_message()
async def on_edited(message: Message):
    # Модерация уже отработала в middleware; здесь ничего не делаем.
    return


# --------------------------------- удаление команд админов после выполнения

class CommandCleanupMiddleware(BaseMiddleware):
    """До обработки скрывает команду всего персонала в групповом чате."""

    async def __call__(self, handler, event, data):
        msg = event
        try:
            cleanup = flag("DELETE_ADMIN_COMMANDS") or flag("ANON_ADMIN")
            if (cleanup and getattr(msg, "text", None)
                    and msg.text.startswith("/")
                    and msg.chat.type in ("group", "supergroup")
                    and msg.from_user
                    and await is_staff_user(msg.chat.id, msg.from_user.id)):
                await bot.delete_message(msg.chat.id, msg.message_id)
        except TelegramBadRequest:
            pass
        return await handler(event, data)


# ----------------------------------------- админ-панель в личке (пароль)

PANEL_TEXT = ("🛠 <b>Панель управления</b>\n"
              "Выбери раздел. ✅ — функция включена, ❌ — выключена.")

PANEL_FLAGS = [
    ("ANTIMAT_ENABLED", "Антимат"),
    ("BLOCK_LINKS", "Ссылки"),
    ("ALLOW_MENTIONS", "Упоминания"),
    ("BLOCK_FORWARDS", "Пересылки"),
    ("BLOCK_CHANNEL_MESSAGES", "Каналы"),
    ("BLOCK_APK", ".apk"),
    ("BLOCK_PREMIUM_EMOJI", "Прем.эмодзи"),
    ("GORE_ON", "Шок-контент/гор"),
    ("DEANON_ENABLED", "Анти-деанон (OCR)"),
    ("TEXT_DEANON_ENABLED", "Анти-деанон текст/угрозы"),
    ("ANTIFLOOD_ENABLED", "Антифлуд"),
    ("ANTIREPEAT_ENABLED", "Анти-повтор"),
    ("TRIGGERS_ENABLED", "Автоответы"),
    ("ANTIRAID_ENABLED", "Антирейд"),
    ("AUTO_CRISIS_ENABLED", "🚨 Автопилот ЧС"),
    ("CHECK_JOIN_NAMES", "Имена"),
    ("CAPTCHA_IMAGE", "Фото-капча (ввод кода)"),
    ("AUTO_ACCEPT", "Автоприём заявок"),
    ("PHONE_VERIFY_ENABLED", "Верификация по номеру"),
    ("DC_CHECK_JOIN", "DC-фильтр (DC5)"),
    ("LOCKDOWN", "🚨 Локдаун"),
    ("WELCOME_ENABLED", "Приветствие"),
    ("REPORT_ENABLED", "Жалобы /report"),
    ("NIGHT_MODE", "Ночной режим"),
    ("QUIET_MODE", "Тихий режим"),
    ("DELETE_SERVICE_MESSAGES", "Чистка сервиса"),
    ("DELETE_ADMIN_COMMANDS", "Чистка команд"),
    ("ANON_ADMIN", "Анонимная модерация"),
    ("DELETE_USER_COMMANDS", "Чистить чужие команды"),
    ("VOTE_ANYONE", "/vb всем"),
    ("NOTIFY_JOINS", "Увед. входы"),
    ("NOTIFY_VIOLATIONS", "Увед. нарушения"),
    ("NOTIFY_REPORTS", "Увед. жалобы"),
    ("RISK_ENABLED", "Риск-фильтр профилей"),
    ("PROBATION_ENABLED", "Наблюдение новичков"),
    ("PROBATION_ON_MEDIA", "Набл.: медиа → мут"),
    ("PROBATION_ON_LINK", "Набл.: ссылка → мут"),
    ("AUTO_CLEAN_DELETED", "Автосвип удалёнок"),
    ("FUZZY_STOPWORDS", "Похожие слова"),
    ("REGULARS_ENABLED", "Мягко к старожилам"),
]

# Числовые настройки, редактируемые из панели.
PANEL_NUMS = [
    ("CAPTCHA_TIMEOUT", "Таймаут капчи (сек)"),
    ("CAPTCHA_STEPS", "Капча: заданий (1-3)"),
    ("CAPTCHA_DIGITS", "Фото-капча: цифр"),
    ("ANTIFLOOD_COUNT", "Антифлуд: сообщений"),
    ("ANTIFLOOD_SECONDS", "Антифлуд: секунд"),
    ("WARN_LIMIT", "Лимит предупреждений"),
    ("RESTRICT_NEWCOMERS_HOURS", "Новичкам без ссылок (ч)"),
    ("NEWCOMER_MEDIA_HOURS", "Новичкам без медиа (ч)"),
    ("PHONE_VERIFY_MINUTES", "Верификация: лимит (мин)"),
    ("DEANON_MIN_HITS", "Деанон: типов данных"),
    ("GORE_THRESHOLD_PCT", "Порог гора (%)"),
    ("ANTIREPEAT_COUNT", "Анти-повтор: одинаковых"),
    ("ACCEPT_BURST_LIMIT", "Заявок до стопа автоприёма"),
    ("ACCEPT_BURST_WINDOW", "Окно всплеска заявок (с)"),
    ("RISK_WATCH_THRESHOLD", "Риск: порог наблюдения"),
    ("RISK_BAN_THRESHOLD", "Риск: порог жёсткий"),
    ("PROBATION_MINUTES", "Наблюдение: минут"),
    ("CLEAN_DELETED_EVERY_HOURS", "Автосвип удалёнок (ч)"),
]

# Действия за фильтры (циклически delete -> warn -> mute -> ban).
PANEL_ACTS = [
    ("LINK_ACTION", "Ссылки"),
    ("FORWARD_ACTION", "Пересылки"),
    ("TEXT_ACTION", "Мат/стоп-слова"),
    ("ANTIFLOOD_ACTION", "Флуд"),
    ("DEANON_ACTION", "Деанон на картинке"),
    ("TEXT_DEANON_ACTION", "Деанон/угроза (текст)"),
    ("WARN_ACTION", "Лимит варнов →"),
    ("RISK_ACTION", "Риск на входе"),
    ("PROBATION_ACTION", "Наблюдение"),
]
ACT_CYCLE = ["delete", "warn", "mute", "ban"]

FLAG_LABELS = dict(PANEL_FLAGS)

# Внутренние права ролей (для редактора «Роли и права» в панели).
PERM_LABELS = [
    ("delete", "🗑 Удаление"),
    ("mute", "🔇 Мут"),
    ("ban", "🔨 Бан"),
    ("warn", "⚠️ Варн"),
    ("kick", "👢 Кик"),
    ("requests", "📥 Заявки/локдаун"),
    ("words", "📋 Стоп-слова"),
    ("manage", "⚙️ Роли/настройки"),
]

# Тумблеры, сгруппированные по разделам (чтобы не «всё в кучу»).
PANEL_CATEGORIES = [
    ("spam", "🛡 Антиспам", ["ANTIMAT_ENABLED", "BLOCK_LINKS", "ALLOW_MENTIONS",
                             "BLOCK_FORWARDS", "BLOCK_CHANNEL_MESSAGES", "BLOCK_APK",
                             "BLOCK_PREMIUM_EMOJI", "GORE_ON", "DEANON_ENABLED",
                             "TEXT_DEANON_ENABLED",
                             "ANTIFLOOD_ENABLED", "ANTIREPEAT_ENABLED", "TRIGGERS_ENABLED",
                             "FUZZY_STOPWORDS"]),
    ("entry", "🚪 Вход и капча", ["CHECK_JOIN_NAMES", "CAPTCHA_IMAGE", "AUTO_ACCEPT",
                                  "DC_CHECK_JOIN", "LOCKDOWN", "WELCOME_ENABLED",
                                  "ANTIRAID_ENABLED", "AUTO_CRISIS_ENABLED",
                                  "PHONE_VERIFY_ENABLED"]),
    ("modes", "🌙 Режимы", ["NIGHT_MODE", "QUIET_MODE", "DELETE_SERVICE_MESSAGES",
                            "DELETE_ADMIN_COMMANDS", "DELETE_USER_COMMANDS",
                            "VOTE_ANYONE", "ANON_ADMIN"]),
    ("notify", "🔔 Уведомления", ["NOTIFY_JOINS", "NOTIFY_VIOLATIONS", "NOTIFY_REPORTS",
                                  "REPORT_ENABLED"]),
    ("profile", "🎯 Профиль-фильтр", ["RISK_ENABLED", "PROBATION_ENABLED",
                                      "PROBATION_ON_MEDIA", "PROBATION_ON_LINK",
                                      "AUTO_CLEAN_DELETED", "REGULARS_ENABLED"]),
]
CAT_FLAGS = {c: keys for c, _, keys in PANEL_CATEGORIES}
CAT_TITLES = {c: title for c, title, _ in PANEL_CATEGORIES}


def panel_keyboard() -> InlineKeyboardMarkup:
    """Главный экран — только разделы, без свалки тумблеров."""
    rows = [[InlineKeyboardButton(text=title, callback_data=f"panel:cat:{c}")]
            for c, title, _ in PANEL_CATEGORIES]
    rows.append([InlineKeyboardButton(text="🔢 Числа", callback_data="panel:nums"),
                 InlineKeyboardButton(text="⚙️ Действия", callback_data="panel:acts")])
    rows.append([InlineKeyboardButton(text="📋 Стоп-слова", callback_data="panel:words"),
                 InlineKeyboardButton(text="📜 Правила", callback_data="panel:rules")])
    rows.append([InlineKeyboardButton(text="📊 Статистика", callback_data="panel:stats"),
                 InlineKeyboardButton(text="📒 Журнал", callback_data="panel:log")])
    rows.append([InlineKeyboardButton(text="💾 Бэкап", callback_data="panel:backup"),
                 InlineKeyboardButton(text="🔄 База картинок", callback_data="panel:reload")])
    rows.append([InlineKeyboardButton(text="🧹 Чистка удалёнок", callback_data="panel:cleandel"),
                 InlineKeyboardButton(text="📞 Верификация номеров", callback_data="panel:phone")])
    rows.append([InlineKeyboardButton(text="🔑 Юзербот (скан)", callback_data="panel:ub"),
                 InlineKeyboardButton(text="🎖 Роли и права", callback_data="panel:roles")])
    rows.append([InlineKeyboardButton(text="🧹 Сброс заявок", callback_data="panel:reqs"),
                 InlineKeyboardButton(text="👑 Владельцы", callback_data="panel:owners")])
    if not IS_CHILD:
        rows.append([InlineKeyboardButton(text="🤖 Мои боты", callback_data="panel:bots")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="panel:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reqs_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for cid, s in pending_requests.items():
        if s:
            rows.append([InlineKeyboardButton(
                text=f"Чат {cid}: {len(s)} заявок — отклонить",
                callback_data=f"panel:reqclr:{cid}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Очередей нет", callback_data="panel:noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_keyboard(cat: str) -> InlineKeyboardMarkup:
    rows, buf = [], []
    for key in CAT_FLAGS.get(cat, []):
        mark = "✅" if flag(key) else "❌"
        buf.append(InlineKeyboardButton(text=f"{mark} {FLAG_LABELS.get(key, key)}",
                                        callback_data=f"panel:t:{key}:{cat}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def backup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Выгрузить настройки", callback_data="panel:bkp_exp")],
        [InlineKeyboardButton(text="⬆️ Загрузить (пришли файл)", callback_data="panel:bkp_imp")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])


def bots_keyboard(owner: int) -> InlineKeyboardMarkup:
    rows = []
    for c in manager.children(owner):
        mark = "🟢" if c["alive"] else "🔴"
        label = f"{mark} @{c['username']}" if c.get("username") else f"{mark} {c['id']}"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"panel:binfo:{c['id']}"),
            InlineKeyboardButton(text="⏹", callback_data=f"panel:bstop:{c['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"panel:bdel:{c['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="panel:addbot")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def roles_keyboard() -> InlineKeyboardMarkup:
    """Список ролей (правка их прав) + переход к назначениям."""
    rows = []
    for name in sorted(config.ROLES, key=lambda n: role_rank(n), reverse=True):
        rows.append([InlineKeyboardButton(
            text=f"{role_badge(name)} {role_title(name)} · ранг {role_rank(name)} ({len(role_perms(name))})",
            callback_data=f"panel:role:{name}")])
    rows.append([InlineKeyboardButton(text="👥 Назначения", callback_data="panel:asg")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_perm_keyboard(role: str) -> InlineKeyboardMarkup:
    """Тумблеры отдельных прав конкретной роли."""
    perms = role_perms(role)
    rows, buf = [], []
    for key, label in PERM_LABELS:
        mark = "✅" if key in perms else "❌"
        buf.append(InlineKeyboardButton(text=f"{mark} {label}",
                                        callback_data=f"panel:rp:{role}:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="✏️ Переименовать ранг",
                                      callback_data=f"panel:rrn:{role}")])
    rows.append([InlineKeyboardButton(text="⬅️ К ролям", callback_data="panel:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def asg_keyboard() -> InlineKeyboardMarkup:
    """Назначенные роли: тап по строке открывает карточку прав юзера; «Выдать роль» — по id."""
    rows = []
    ordered = sorted(storage.roles_all().items(), key=lambda kv: role_rank(kv[1]), reverse=True)
    for u, role in ordered:
        try:
            who = cached_name(int(u))
        except (ValueError, TypeError):
            who = str(u)
        # ✏️ если у человека личный оверрайд прав (отличается от роли).
        custom = "✏️" if storage.get_user_perms_override(int(u)) is not None else "👤"
        rows.append([InlineKeyboardButton(text=f"{custom} {role_badge(role)} {role_title(role)} — {who}",
                                          callback_data=f"panel:uperm:{u}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Никому не назначено", callback_data="panel:noop")])
    rows.append([InlineKeyboardButton(text="➕ Выдать роль", callback_data="panel:asgnew")])
    rows.append([InlineKeyboardButton(text="⬅️ К ролям", callback_data="panel:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_perm_keyboard(uid: int) -> InlineKeyboardMarkup:
    """Тумблеры прав ЛИЧНО для юзера (поверх роли) + сброс к роли и снятие роли."""
    perms = effective_perms(uid)
    has_override = storage.get_user_perms_override(uid) is not None
    rows, buf = [], []
    for key, label in PERM_LABELS:
        mark = "✅" if key in perms else "❌"
        buf.append(InlineKeyboardButton(text=f"{mark} {label}",
                                        callback_data=f"panel:up:{uid}:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    if has_override:
        rows.append([InlineKeyboardButton(text="↩️ Сбросить к правам роли",
                                          callback_data=f"panel:upreset:{uid}")])
    rows.append([InlineKeyboardButton(text="❌ Снять роль",
                                      callback_data=f"panel:unrole:{uid}")])
    rows.append([InlineKeyboardButton(text="⬅️ К назначениям", callback_data="panel:asg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def asg_pick_keyboard() -> InlineKeyboardMarkup:
    """Выбор роли для назначения по id."""
    rows = [[InlineKeyboardButton(text=f"{role_badge(name)} {role_title(name)}",
                                  callback_data=f"panel:asgrole:{name}")]
            for name in config.ROLES]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:asg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owners_keyboard() -> InlineKeyboardMarkup:
    """Владельцы: снять (❌), сделать себя владельцем, добавить по id."""
    rows = []
    for u in storage.owners_all():
        rows.append([InlineKeyboardButton(text=f"❌ {u}", callback_data=f"panel:ownrm:{u}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Владельцев пока нет", callback_data="panel:noop")])
    rows.append([InlineKeyboardButton(text="👑 Сделать меня владельцем", callback_data="panel:ownme")])
    rows.append([InlineKeyboardButton(text="➕ Добавить по id", callback_data="panel:ownadd")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])


def words_panel_text() -> str:
    total = len(storage.stopwords())
    hidden = len(storage.hidden_words())
    return (f"📋 <b>Стоп-слова</b> ({total}, из них скрытых 🕵️ {hidden})\n"
            "👁 — открытое (в чате видно причину), 🕵️ — скрытое (причина не "
            "показывается). Нажми 👁/🕵️ — переключить, ❌ — удалить.")


def words_keyboard() -> InlineKeyboardMarkup:
    # Каждая строка: [🕵️/👁 переключить скрытость] [❌ удалить слово].
    rows = []
    for i, w in enumerate(storage.stopwords()):
        hidden = storage.is_hidden_word(w)
        eye = "🕵️" if hidden else "👁"
        rows.append([
            InlineKeyboardButton(text=f"{eye} {w[:22]}", callback_data=f"panel:hw:{i}"),
            InlineKeyboardButton(text="❌", callback_data=f"panel:dw:{i}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Открытое", callback_data="panel:addword"),
                 InlineKeyboardButton(text="🕵️ Скрытое", callback_data="panel:addwordh")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_panel_text() -> str:
    allow = ", ".join(_prefix_list("PHONE_ALLOW_PREFIXES", config.PHONE_ALLOW_PREFIXES)) or "—"
    review = ", ".join(_prefix_list("PHONE_REVIEW_PREFIXES", config.PHONE_REVIEW_PREFIXES)) or "—"
    onoff = "включена ✅" if flag("PHONE_VERIFY_ENABLED") else "выключена ❌"
    fail = storage.get_str("PHONE_VERIFY_FAIL_ACTION", config.PHONE_VERIFY_FAIL_ACTION)
    return ("📞 <b>Верификация по номеру</b> — " + onoff + "\n\n"
            f"🟢 <b>Пускать сразу</b> (100% вход): {esc(allow)}\n"
            f"🟡 <b>На ручную проверку</b>: {esc(review)}\n"
            f"🔴 Остальные — отказ (действие: <b>{esc(fail)}</b>)\n\n"
            "Нажми префикс, чтобы удалить его из списка. Списки видны только тут — "
            "пользователям они не показываются.")


def phone_panel_keyboard() -> InlineKeyboardMarkup:
    allow = _prefix_list("PHONE_ALLOW_PREFIXES", config.PHONE_ALLOW_PREFIXES)
    review = _prefix_list("PHONE_REVIEW_PREFIXES", config.PHONE_REVIEW_PREFIXES)
    rows = []
    for i, p in enumerate(allow):
        rows.append([InlineKeyboardButton(text=f"🟢 {p}  ❌", callback_data=f"panel:phdel:a:{i}")])
    for i, p in enumerate(review):
        rows.append([InlineKeyboardButton(text=f"🟡 {p}  ❌", callback_data=f"panel:phdel:r:{i}")])
    rows.append([InlineKeyboardButton(text="➕ Пускать сразу", callback_data="panel:phadd:a"),
                 InlineKeyboardButton(text="➕ На проверку", callback_data="panel:phadd:r")])
    fail = storage.get_str("PHONE_VERIFY_FAIL_ACTION", config.PHONE_VERIFY_FAIL_ACTION)
    rows.append([InlineKeyboardButton(text=f"🔴 Отказ: {fail} (тап — сменить)",
                                      callback_data="panel:phfail")])
    rows.append([InlineKeyboardButton(text="✏️ Политика конфиденциальности",
                                      callback_data="panel:phpriv")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nums_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{label}: {num(key)}", callback_data=f"panel:sn:{key}")]
            for key, label in PANEL_NUMS]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def acts_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{label}: {action_for(key)}", callback_data=f"panel:ac:{key}")]
            for key, label in PANEL_ACTS]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _child_stats(bid: str) -> dict:
    """Прочитать статистику дочернего бота из его data.json."""
    import json
    path = os.path.join(manager.CHILDREN_DIR, bid, "data.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("stats", {})
    except Exception:
        return {}


async def open_panel(chat_id: int):
    await bot.send_message(chat_id, PANEL_TEXT, reply_markup=panel_keyboard())


# --------------------- верификация по номеру телефона в ЛС ---------------------

def phone_kb() -> ReplyKeyboardMarkup:
    """Клавиатура с одной кнопкой «Поделиться номером» (request_contact)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться моим номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="Нажми кнопку ниже 👇")


async def begin_phone_verify(message: Message) -> None:
    """Юзер открыл бота по deep-link из группы — показываем политику и просим номер."""
    uid = message.from_user.id
    if storage.is_phone_verified(uid):
        # Уже верифицирован глобально — сразу впустим там, где ждёт (если ждёт).
        await message.answer("✅ Ты уже подтверждён. Спасибо!",
                             reply_markup=ReplyKeyboardRemove())
        await _release_after_verify(uid, message.from_user)
        return
    await message.answer(
        storage.get_str("PRIVACY_TEXT", config.PRIVACY_TEXT),
        disable_web_page_preview=True)
    await message.answer(
        "👋 Чтобы получить доступ к чату, подтверди, что ты живой человек.\n\n"
        "Нажми кнопку <b>«Поделиться моим номером»</b> ниже. "
        "Номер проверится автоматически, <b>никому не показывается</b> и "
        "третьим лицам не передаётся. Полностью его мы не храним.",
        reply_markup=phone_kb())


def _prefix_list(name: str, default: list) -> list[str]:
    """Список префиксов из storage-оверрайда (CSV) либо из config."""
    raw = storage.get_str(name, ",".join(default))
    return [p.strip() for p in raw.split(",") if p.strip()]


def phone_tier(phone: str) -> str:
    """Куда попадает номер: 'allow' (пускать сразу) | 'review' (ручное одобрение)
    | 'deny' (отказ). Длинные префиксы проверяем раньше (более специфичные)."""
    allow = _prefix_list("PHONE_ALLOW_PREFIXES", config.PHONE_ALLOW_PREFIXES)
    review = _prefix_list("PHONE_REVIEW_PREFIXES", config.PHONE_REVIEW_PREFIXES)
    best, tier = "", "deny"
    for p in allow:
        if phone.startswith(p) and len(p) > len(best):
            best, tier = p, "allow"
    for p in review:
        if phone.startswith(p) and len(p) > len(best):
            best, tier = p, "review"
    return tier


def _normalize_phone(raw: str) -> str:
    """'79991234567' / '+7 999 ...' -> '+79991234567' (только + и цифры)."""
    digits = re.sub(r"\D", "", raw or "")
    return "+" + digits if digits else ""


async def _release_after_verify(uid: int, user) -> None:
    """Впустить юзера в группу, где он ждал верификацию (если такая есть)."""
    st = verify_wait.get(uid)
    pend = storage.get_pending_verify(uid)
    chat_id = (st or {}).get("chat_id") or (pend or {}).get("chat")
    task = (st or {}).get("task")
    if task and not task.done():
        task.cancel()
    notice_id = (st or {}).get("notice_id") or (pend or {}).get("notice")
    verify_wait.pop(uid, None)
    storage.clear_pending_verify(uid)
    if not chat_id:
        return
    if notice_id:
        try:
            await bot.delete_message(chat_id, notice_id)
        except TelegramBadRequest:
            pass
    await grant_member_access(chat_id, user)
    log.info("Юзер %s верифицирован по номеру — доступ в чат %s выдан.", uid, chat_id)


@dp.message(F.contact, F.chat.type == "private")
async def on_contact(message: Message) -> None:
    """Юзер прислал контакт в ЛС — три уровня: пустить / на ручное одобрение / отказ."""
    if not flag("PHONE_VERIFY_ENABLED"):
        return
    uid = message.from_user.id
    contact = message.contact
    # Контакт должен быть СВОИМ (нельзя переслать чужую «правильную» визитку).
    if config.PHONE_REQUIRE_OWN_CONTACT and contact.user_id != uid:
        await message.answer(
            "❗️ Это не твой номер. Пожалуйста, нажми кнопку "
            "<b>«Поделиться моим номером»</b> — так отправится именно твой контакт.",
            reply_markup=phone_kb())
        return

    phone = _normalize_phone(contact.phone_number)
    tier = phone_tier(phone)

    if tier == "allow":
        matched = phone[:2]
        storage.set_phone_verified(uid, matched, phone[-2:], fmt_when())
        await message.answer("✅ Готово! Ты подтверждён. Добро пожаловать 🙂",
                             reply_markup=ReplyKeyboardRemove())
        await _release_after_verify(uid, message.from_user)
        audit("верификация", "номер подтверждён", uid, message.from_user.full_name)
    elif tier == "review":
        await message.answer(
            "⏳ Спасибо! Твоя заявка отправлена на проверку модератору. "
            "Как только её одобрят — ты сможешь писать в чат. Обычно это быстро.",
            reply_markup=ReplyKeyboardRemove())
        await _send_for_review(uid, message.from_user, phone)
    else:
        # Нейтральный отказ — НЕ раскрываем, какие коды принимаются.
        await message.answer(
            "🚫 Не удалось подтвердить номер. Доступ к чату не выдан.\n"
            "Если считаешь, что это ошибка — обратись к администратору группы.",
            reply_markup=ReplyKeyboardRemove())
        await _fail_verify(uid, message.from_user, phone[-4:] if phone else "—")


def verify_review_kb(chat_id: int, uid: int) -> InlineKeyboardMarkup:
    p = f"{chat_id}:{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Впустить", callback_data=f"vrf:ok:{p}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"vrf:no:{p}")]])


async def _send_for_review(uid: int, user, phone: str) -> None:
    """«Серый» номер: держим юзера в муте, шлём админам карточку с кнопками."""
    st = verify_wait.get(uid)
    pend = storage.get_pending_verify(uid)
    chat_id = (st or {}).get("chat_id") or (pend or {}).get("chat")
    task = (st or {}).get("task")
    if task and not task.done():
        task.cancel()
    prefix, tail = phone[:3], (phone[-2:] if phone else "")
    notice_id = (st or {}).get("notice_id") or (pend or {}).get("notice")
    storage.set_pending_review(uid, chat_id, notice_id, prefix, tail, fmt_when())
    verify_wait.pop(uid, None)
    name = getattr(user, "full_name", "") or "пользователь"
    card = event_card("🕵 Заявка на верификацию (ручная проверка)", user,
                      reason=f"номер {prefix}…{tail}; чат {chat_id}")
    kb = verify_review_kb(chat_id, uid) if chat_id else None
    sent_any = False
    for admin_id in list(panel_auth):
        try:
            await bot.send_message(admin_id, card, reply_markup=kb)
            sent_any = True
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    # Продублируем в лог-чат/группу, чтобы не потерялось, если панель никто не открыл.
    if chat_id and (config.LOG_CHAT_ID or not sent_any):
        await report(chat_id,
                     f"🕵 {id_mention(uid, name)} ждёт ручной верификации "
                     f"(номер {esc(prefix)}…{esc(tail)}).", kb)
    audit("верификация", f"на ручную проверку {prefix}…{tail}", uid, name)
    log.info("Юзер %s (номер %s…%s) отправлен на ручное одобрение.", uid, prefix, tail)


async def _fail_verify(uid: int, user, tail: str) -> None:
    """Номер не из списков: применяем PHONE_VERIFY_FAIL_ACTION в группе."""
    st = verify_wait.get(uid)
    pend = storage.get_pending_verify(uid)
    chat_id = (st or {}).get("chat_id") or (pend or {}).get("chat")
    task = (st or {}).get("task")
    if task and not task.done():
        task.cancel()
    act = storage.get_str("PHONE_VERIFY_FAIL_ACTION", config.PHONE_VERIFY_FAIL_ACTION)
    if chat_id:
        if act == "ban":
            await ban_user(chat_id, uid)
        elif act == "kick":
            await ban_user(chat_id, uid)
            try:
                await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
            except TelegramBadRequest:
                pass
        # act == "mute": юзер и так в муте после капчи — оставляем как есть.
        notice_id = (st or {}).get("notice_id") or (pend or {}).get("notice")
        if notice_id:
            try:
                await bot.delete_message(chat_id, notice_id)
            except TelegramBadRequest:
                pass
        await report(chat_id,
                     f"🚫 {id_mention(uid, (st or {}).get('name') or 'пользователь')} "
                     f"не прошёл верификацию по номеру (код не из белого списка, "
                     f"…{esc(tail)}) — {act}.")
    audit("верификация", f"отказ по номеру …{tail} ({act})", uid,
          getattr(user, "full_name", ""))
    if flag("NOTIFY_VIOLATIONS") and user is not None:
        await notify_panel(event_card("🚫 Верификация: чужая страна", user,
                                      reason=f"номер …{tail}, действие: {act}"))
    verify_wait.pop(uid, None)
    storage.clear_pending_verify(uid)


class PrivacyGate(BaseMiddleware):
    """Глушит посторонних в ЛС: чужой не должен получить ответ на /admin и прочее,
    чтобы случайно не наткнуться на вход в панель. Свои (пароль/владелец/уже
    в панели) и новички на верификации по номеру проходят как обычно."""
    async def __call__(self, handler, event, data):
        msg = event
        chat = getattr(msg, "chat", None)
        if chat is None or chat.type != "private" or not getattr(msg, "from_user", None):
            return await handler(event, data)
        uid = msg.from_user.id
        if uid in panel_auth or storage.is_owner(uid):
            return await handler(event, data)
        text = (getattr(msg, "text", None) or "").strip()
        if text == config.PANEL_PASSWORD:
            return await handler(event, data)
        if text.startswith("/start") and "verify" in text:
            return await handler(event, data)
        # Новичок реально в процессе верификации по номеру — пропускаем.
        if (flag("PHONE_VERIFY_ENABLED") and not storage.is_phone_verified(uid)
                and (uid in verify_wait or storage.get_pending_verify(uid))):
            return await handler(event, data)
        # Посторонний: молчим — панель не светим.
        return


@dp.message(Command("admin", "start"), F.chat.type == "private")
async def panel_entry(message: Message):
    uid = message.from_user.id
    # Deep-link верификации по номеру: /start verify_<chat_id> из кнопки в группе.
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        payload = parts[1].strip()
    if payload.startswith("verify"):
        await begin_phone_verify(message)
        return
    if uid in panel_auth:
        await open_panel(message.chat.id)
    else:
        panel_state[uid] = "await_pass"
        await message.answer("🔒 Введи пароль для доступа к панели управления:")


@dp.message(F.chat.type == "private")
async def panel_private(message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    # Новичок, ожидающий верификацию (не знает пароль панели): не пугаем «паролем».
    if (uid not in panel_auth and flag("PHONE_VERIFY_ENABLED")
            and not storage.is_phone_verified(uid)):
        pend = storage.get_pending_verify(uid)
        if pend and pend.get("review"):
            await message.answer("⏳ Твоя заявка на проверке у модератора. "
                                 "Как только её рассмотрят — придёт ответ.")
            return
        if uid in verify_wait or pend:
            await message.answer(
                "Чтобы получить доступ к чату, нажми кнопку "
                "<b>«Поделиться моим номером»</b> ниже 👇",
                reply_markup=phone_kb())
            return
    if uid not in panel_auth:
        if message.text and message.text.strip() == config.PANEL_PASSWORD:
            panel_auth.add(uid)
            storage.add_owner(uid)  # знавший пароль = владелец: PrivacyGate его теперь узнаёт
            panel_state.pop(uid, None)
            await message.answer("✅ Доступ открыт.")
            await open_panel(message.chat.id)
        else:
            await message.answer("🔒 Неверный пароль. Попробуй ещё раз:")
        return
    st = panel_state.get(uid)

    # Восстановление настроек из присланного файла.
    if st == "restore" and message.document:
        panel_state.pop(uid, None)
        try:
            data = (await bot.download(message.document)).read()
            import json
            json.loads(data.decode("utf-8"))  # проверка, что валидный JSON
            with open(storage._PATH, "wb") as f:
                f.write(data)
            storage.load()
            await message.answer("✅ Настройки восстановлены из файла.")
        except Exception as e:
            await message.answer(f"Не вышло (нужен валидный data.json): {esc(str(e))}")
        await open_panel(message.chat.id)
        return

    # Двухшаговое создание дочернего бота: токен -> свой пароль.
    if st == "add_bot" and message.text:
        await _bot_token_step(message)
        return
    if st == "add_bot_pass" and message.text:
        await _bot_password_step(message)
        return

    # Скрытый пошаговый логин юзербота: номер -> код -> (пароль 2FA).
    if st in ("ub_phone", "ub_code", "ub_password") and message.text:
        await _ub_login_step(message, st)
        return

    if st and message.text:
        val = message.text.strip()
        panel_state.pop(uid, None)
        if val.startswith("/"):
            await message.answer("Отменено.")
        elif st in ("add_word", "add_word_hidden"):
            hidden = st == "add_word_hidden"
            ok = storage.add_stopword(val, hidden=hidden)
            tag = " 🕵️ (скрытое)" if hidden else ""
            await message.answer(f"✅ Добавлено «{esc(val)}»{tag}." if ok else "Уже есть.")
        elif st == "set_rules":
            storage.set_rules(message_markdown(message).strip() or val)
            await message.answer("✅ Правила сохранены.")
        elif st == "set_privacy":
            if val == "-":
                storage.set_str("PRIVACY_TEXT", config.PRIVACY_TEXT)
                await message.answer("✅ Политика сброшена к стандартной. Проверь: /privacy")
            else:
                storage.set_str("PRIVACY_TEXT", val)
                await message.answer("✅ Политика конфиденциальности сохранена. Проверь: /privacy",
                                     disable_web_page_preview=True)
        elif st in ("phadd_a", "phadd_r"):
            name = "PHONE_ALLOW_PREFIXES" if st == "phadd_a" else "PHONE_REVIEW_PREFIXES"
            default = (config.PHONE_ALLOW_PREFIXES if st == "phadd_a"
                       else config.PHONE_REVIEW_PREFIXES)
            cur = _prefix_list(name, default)
            added = []
            for token in re.split(r"[,\s]+", val):
                token = token.strip()
                if not token:
                    continue
                if not token.startswith("+"):
                    token = "+" + token
                if re.fullmatch(r"\+\d{1,6}", token) and token not in cur:
                    cur.append(token)
                    added.append(token)
            if added:
                storage.set_str(name, ",".join(cur))
                await message.answer(f"✅ Добавлено: {esc(', '.join(added))}")
            else:
                await message.answer("Ничего не добавил (нужен код вида +81; либо уже есть).")
        elif st.startswith("rename_role:"):
            role = st.split(":", 1)[1]
            if role not in config.ROLES:
                await message.answer("Роль не найдена.")
            elif val == "-":
                storage.set_role_title(role, None)
                await message.answer(f"✅ Название сброшено: {role_label(role)}")
            else:
                storage.set_role_title(role, val)
                await message.answer(f"✅ Ранг теперь отображается как {role_label(role)}")
        elif st.startswith("setnum:"):
            key = st.split(":", 1)[1]
            if not val.lstrip("-").isdigit():
                await message.answer("Нужно число.")
            elif key == "CAPTCHA_STEPS" and not (1 <= int(val) <= 3):
                await message.answer("Капча: допустимо 1, 2 или 3 задания.")
            else:
                storage.set_num(key, int(val))
                await message.answer(f"✅ {key} = {val}.")
        elif st == "add_owner":
            if val.lstrip("-").isdigit():
                added = storage.add_owner(int(val))
                await message.answer("✅ Владелец добавлен." if added else "Уже владелец.")
            else:
                await message.answer("Нужен числовой id.")
        elif st.startswith("assign_uid:"):
            role = st.split(":", 1)[1]
            if not val.lstrip("-").isdigit():
                await message.answer("Нужен числовой id.")
            elif role not in config.ROLES:
                await message.answer("Роль не найдена.")
            else:
                storage.set_role(int(val), role)
                await message.answer(f"✅ <code>{esc(val)}</code> — роль «{esc(role)}».")
        await open_panel(message.chat.id)
        return
    await open_panel(message.chat.id)


async def _ub_login_step(message: Message, st: str):
    """Скрытый логин юзербота по шагам: ub_phone -> ub_code -> ub_password.

    Сообщения с номером/кодом/паролем сразу удаляем, чтобы не висели в истории.
    """
    uid = message.from_user.id
    raw = (message.text or "").strip()
    try:
        await message.delete()                    # прячем секрет из чата
    except TelegramBadRequest:
        pass

    if raw.startswith("/") or raw.lower() in ("отмена", "cancel", "стоп"):
        state = ub_login.pop(uid, None)
        if state:
            await userbot.login_cancel(state)
        panel_state.pop(uid, None)
        await bot.send_message(message.chat.id, "Отменено.")
        await open_panel(message.chat.id)
        return

    if st == "ub_phone":
        await bot.send_message(message.chat.id, "⏳ Запрашиваю код…")
        state, info = await userbot.login_start(raw)
        if state is None:
            panel_state.pop(uid, None)
            await bot.send_message(message.chat.id, f"⚠️ {esc(info)}")
            await open_panel(message.chat.id)
            return
        ub_login[uid] = state
        panel_state[uid] = "ub_code"
        await bot.send_message(
            message.chat.id,
            "✅ " + esc(info) + "\n\n🔢 Введи код из Telegram <b>с чёрточками</b>: "
            "<code>1-2-3-4-5</code> (иначе Telegram аннулирует код). Лишнее уберу сам.")
        return

    state = ub_login.get(uid)
    if state is None:
        panel_state.pop(uid, None)
        await bot.send_message(message.chat.id, "Сессия входа потерялась, начни заново.")
        await open_panel(message.chat.id)
        return

    if st == "ub_code":
        code = "".join(ch for ch in raw if ch.isdigit())
        if not code:
            await bot.send_message(message.chat.id, "Не вижу цифр. Пришли код вида 1-2-3-4-5:")
            return
        res = await userbot.login_code(state, code)
        if res == "need_password":
            panel_state[uid] = "ub_password"
            await bot.send_message(message.chat.id,
                                   "🔐 На аккаунте включён облачный пароль (2FA). "
                                   "Пришли его одним сообщением (удалю сразу):")
            return
    else:  # ub_password
        res = await userbot.login_password(state, raw)

    ub_login.pop(uid, None)
    panel_state.pop(uid, None)
    if res.startswith("ok:"):
        who = res[3:]
        await bot.send_message(
            message.chat.id,
            f"✅ Юзербот вошёл как <b>{esc(who)}</b>. Сессия сохранена.\n"
            "Теперь добавь этот аккаунт в группу (с правом удалять участников) и "
            "запусти <code>/scanall</code> в чате.")
    else:
        err = res[6:] if res.startswith("error:") else res
        await bot.send_message(message.chat.id, f"⚠️ Не вышло войти: {esc(err)}")
    await open_panel(message.chat.id)


async def _bot_token_step(message: Message):
    """Шаг 1: принять токен, проверить, спросить пароль для НОВОГО бота."""
    uid = message.from_user.id
    token = message.text.strip()
    if token.startswith("/"):
        panel_state.pop(uid, None)
        await message.answer("Отменено.")
        await open_panel(message.chat.id)
        return
    if IS_CHILD or not manager.valid_token(token):
        panel_state.pop(uid, None)
        await message.answer("Это не похоже на токен бота. Формат: 123456:AA…")
        await open_panel(message.chat.id)
        return
    from aiogram import Bot as _Bot
    test = _Bot(token)
    try:
        me = await test.get_me()
    except Exception:
        panel_state.pop(uid, None)
        await message.answer("Токен недействителен (getMe не прошёл).")
        await open_panel(message.chat.id)
        return
    finally:
        await test.session.close()
    panel_newbot[uid] = {"token": token, "username": me.username or ""}
    panel_state[uid] = "add_bot_pass"
    await message.answer(
        f"Бот @{esc(me.username)} проверен ✅\n"
        "Теперь придумай <b>пароль</b> для этого нового бота (он будет свой, "
        "не общий) — пришли его одним сообщением:")


async def _bot_password_step(message: Message):
    """Шаг 2: задать пароль и запустить нового бота под этим владельцем."""
    uid = message.from_user.id
    pw = message.text.strip()
    draft = panel_newbot.pop(uid, None)
    panel_state.pop(uid, None)
    if not draft:
        await open_panel(message.chat.id)
        return
    if pw.startswith("/") or len(pw) < 3:
        await message.answer("Пароль слишком короткий (минимум 3 символа). Создание отменено.")
        await open_panel(message.chat.id)
        return
    if manager.add(draft["token"], draft["username"], owner=uid, password=pw):
        await message.answer(
            f"✅ Бот @{esc(draft['username'])} создан и запущен.\n"
            f"Это <b>отдельный</b> бот: свой пароль <code>{esc(pw)}</code>, "
            "своя база и настройки. Заходи в него и открывай /admin с этим паролем.")
    else:
        await message.answer("Такой бот уже есть в списке.")
    await open_panel(message.chat.id)


@dp.callback_query(F.data.startswith("panel:"))
async def panel_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in panel_auth:
        await cb.answer("Нет доступа. Открой панель командой /admin в личке.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "cat":
        cat = parts[2]
        await cb.answer()
        try:
            await cb.message.edit_text(f"{CAT_TITLES.get(cat, 'Раздел')}\nЖми, чтобы переключить:",
                                       reply_markup=category_keyboard(cat))
        except TelegramBadRequest:
            pass
    elif action == "t":
        key = parts[2]
        cat = parts[3] if len(parts) > 3 else None
        storage.set_flag(key, not flag(key))
        await cb.answer("Переключено")
        kb = category_keyboard(cat) if cat else panel_keyboard()
        title = (f"{CAT_TITLES.get(cat, 'Раздел')}\nЖми, чтобы переключить:"
                 if cat else PANEL_TEXT)
        try:
            await cb.message.edit_text(title, reply_markup=kb)
        except TelegramBadRequest:
            pass
    elif action == "back":
        await cb.answer()
        try:
            await cb.message.edit_text(PANEL_TEXT, reply_markup=panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "backup":
        await cb.answer()
        try:
            await cb.message.edit_text(
                "💾 <b>Бэкап настроек</b>\nВыгрузи файл настроек или загрузи свой "
                "(стоп-слова, варны, флаги, правила).", reply_markup=backup_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "bkp_exp":
        await cb.answer("Отправляю файл…")
        from aiogram.types import FSInputFile
        try:
            storage.save()  # сбросить актуальное на диск
            await bot.send_document(cb.message.chat.id, FSInputFile(storage._PATH, filename="data.json"))
        except Exception as e:
            await bot.send_message(cb.message.chat.id, f"Не вышло: {esc(str(e))}")
    elif action == "bkp_imp":
        panel_state[uid] = "restore"
        await cb.answer()
        await bot.send_message(cb.message.chat.id,
                               "⬆️ Пришли файл <code>data.json</code> документом — заменю настройки.")
    elif action == "stats":
        await cb.answer()
        try:
            await cb.message.edit_text(stats_text(), reply_markup=back_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "words":
        await cb.answer()
        try:
            await cb.message.edit_text(words_panel_text(), reply_markup=words_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "addword":
        panel_state[uid] = "add_word"
        await cb.answer()
        await bot.send_message(cb.message.chat.id, "✍️ Напиши новое стоп-слово одним сообщением:")
    elif action == "addwordh":
        panel_state[uid] = "add_word_hidden"
        await cb.answer()
        await bot.send_message(cb.message.chat.id,
                               "🕵️ Напиши новое СКРЫТОЕ стоп-слово одним сообщением "
                               "(в чате не показывается):")
    elif action == "dw":
        words = storage.stopwords()
        i = int(parts[2])
        if 0 <= i < len(words):
            storage.del_stopword(words[i])
        await cb.answer("Удалено")
        try:
            await cb.message.edit_text(words_panel_text(), reply_markup=words_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "hw":
        words = storage.stopwords()
        i = int(parts[2])
        if 0 <= i < len(words):
            new_hidden = not storage.is_hidden_word(words[i])
            storage.set_hidden_word(words[i], new_hidden)
            await cb.answer("Скрыто 🕵️" if new_hidden else "Открыто 👁")
        else:
            await cb.answer()
        try:
            await cb.message.edit_text(words_panel_text(), reply_markup=words_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "reload":
        load_reference_hashes()
        await cb.answer(f"База обновлена: {len(ref_hashes)} картинок.", show_alert=True)
    elif action == "phone":
        await cb.answer()
        try:
            await cb.message.edit_text(phone_panel_text(), reply_markup=phone_panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "phdel":
        lst, i = parts[2], int(parts[3])
        name = "PHONE_ALLOW_PREFIXES" if lst == "a" else "PHONE_REVIEW_PREFIXES"
        default = config.PHONE_ALLOW_PREFIXES if lst == "a" else config.PHONE_REVIEW_PREFIXES
        cur = _prefix_list(name, default)
        if 0 <= i < len(cur):
            cur.pop(i)
            storage.set_str(name, ",".join(cur))
        await cb.answer("Удалено")
        try:
            await cb.message.edit_text(phone_panel_text(), reply_markup=phone_panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "phadd":
        panel_state[uid] = "phadd_a" if parts[2] == "a" else "phadd_r"
        await cb.answer()
        where = "🟢 пускать сразу" if parts[2] == "a" else "🟡 на ручную проверку"
        await bot.send_message(
            cb.message.chat.id,
            f"✍️ Пришли код страны для списка «{where}» в формате <code>+81</code> "
            "(можно несколько через запятую):")
    elif action == "phfail":
        cur = storage.get_str("PHONE_VERIFY_FAIL_ACTION", config.PHONE_VERIFY_FAIL_ACTION)
        cyc = ["mute", "kick", "ban"]
        nxt = cyc[(cyc.index(cur) + 1) % len(cyc)] if cur in cyc else cyc[0]
        storage.set_str("PHONE_VERIFY_FAIL_ACTION", nxt)
        await cb.answer(f"Отказ: {nxt}")
        try:
            await cb.message.edit_text(phone_panel_text(), reply_markup=phone_panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "phpriv":
        panel_state[uid] = "set_privacy"
        await cb.answer()
        cur = storage.get_str("PRIVACY_TEXT", config.PRIVACY_TEXT)
        await bot.send_message(
            cb.message.chat.id,
            "✏️ Пришли новый текст политики конфиденциальности одним сообщением "
            "(HTML-теги можно). Сброс к стандартному — пришли <code>-</code>.\n\n"
            f"Текущий:\n{cur}", disable_web_page_preview=True)
    elif action == "cleandel":
        await cb.answer("Сканирую удалёнок…")
        chats = list(known_chats)
        if not chats:
            await bot.send_message(cb.message.chat.id,
                                   "Пока не видел ни одного чата с активностью — нечего чистить.")
            return
        total = {"scanned": 0, "deleted": 0, "kicked": 0}
        lines = []
        for chat_id in chats:
            res = await sweep_deleted(chat_id, do_kick=True)
            for k in total:
                total[k] += res[k]
            if res["kicked"]:
                lines.append(f"• <code>{chat_id}</code>: выкинуто {res['kicked']}")
        if total["kicked"]:
            audit("чистка", f"из панели: кикнуто удалёнок {total['kicked']}", uid)
        body = ("\n".join(lines) + "\n\n") if lines else ""
        extra = ("\n💡 Для полного скана всех участников — /scanall в чате (нужен юзербот)."
                 if userbot.available() else
                 "\nBot API видит только виденных — полный скан требует юзербота (userbot.py).")
        await bot.send_message(
            cb.message.chat.id,
            f"🧹 <b>Чистка удалёнок</b> по {len(chats)} чатам.\n{body}"
            f"Проверено: <b>{total['scanned']}</b>, удалёнок: <b>{total['deleted']}</b>, "
            f"выкинуто: <b>{total['kicked']}</b>.{extra}")
    elif action == "ub":
        await cb.answer()
        ready = userbot.available()
        auth = await userbot.session_authorized_async() if ready else False
        st_line = esc(userbot.status())
        if auth:
            body = ("🔑 <b>Юзербот</b>\nСтатус: готов, сессия авторизована ✅\n"
                    "Полный скан участников: команда <code>/scanall</code> в чате "
                    "(юзербот должен быть в группе с правом удалять участников).")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Выйти из аккаунта", callback_data="panel:ublogout")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])
        elif ready:
            body = ("🔑 <b>Юзербот</b>\nСтатус: " + st_line + ", но <b>вход не выполнен</b>.\n\n"
                    "Войду прямо здесь по шагам: номер → код → (пароль 2FA).\n"
                    "⚠️ Код Telegram присылай <b>с чёрточками</b> (1-2-3-4-5) — иначе "
                    "Telegram его аннулирует за пересылку в чате. Я уберу лишнее сам.")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Войти в аккаунт", callback_data="panel:ublogin")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])
        else:
            body = ("🔑 <b>Юзербот недоступен</b>\nСтатус: " + st_line + ".\n"
                    "Нужен Telethon (pip install telethon) и api_id/api_hash "
                    "(secrets_local.py / config.USERBOT_*).")
            kb = back_keyboard()
        try:
            await cb.message.edit_text(body, reply_markup=kb)
        except TelegramBadRequest:
            pass
    elif action == "ublogin":
        panel_state[uid] = "ub_phone"
        await cb.answer()
        await bot.send_message(cb.message.chat.id,
                               "📱 Пришли номер телефона аккаунта-юзербота в формате "
                               "<code>+79991234567</code>:")
    elif action == "ublogout":
        await cb.answer("Выхожу…")
        msg = await userbot.logout()
        await bot.send_message(cb.message.chat.id, esc(msg))
    elif action == "reqs":
        await cb.answer()
        total = sum(len(s) for s in pending_requests.values())
        try:
            await cb.message.edit_text(
                f"🧹 <b>Сброс заявок</b>\nВ очереди всего: {total}. "
                "Жми чат — отклоню все его заявки, что видел бот.\n"
                "(Полная зачистка всех висящих — purge_raid.py.)",
                reply_markup=reqs_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "reqclr":
        gid = int(parts[2])
        n = len(pending_requests.get(gid, set()))
        await cb.answer(f"Отклоняю {n}…")
        declined = await clear_requests(gid, uid)
        try:
            await cb.message.edit_text(f"✅ Чат {gid}: отклонено {declined} заявок.",
                                       reply_markup=reqs_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "nums":
        await cb.answer()
        try:
            await cb.message.edit_text("🔢 Числовые настройки. Нажми, чтобы изменить:",
                                       reply_markup=nums_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "sn":
        key = parts[2]
        panel_state[uid] = f"setnum:{key}"
        await cb.answer()
        await bot.send_message(cb.message.chat.id, f"✍️ Введи новое значение для <b>{esc(key)}</b> числом:")
    elif action == "acts":
        await cb.answer()
        try:
            await cb.message.edit_text("⚙️ Действие за каждый фильтр (тап — следующее):",
                                       reply_markup=acts_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "ac":
        key = parts[2]
        cur = action_for(key)
        if key == "WARN_ACTION":
            cycle = ["mute", "ban"]
        elif key == "RISK_ACTION":
            cycle = ["mute", "ban", "captcha"]
        elif key == "PROBATION_ACTION":
            cycle = ["mute", "ban"]
        else:
            cycle = ACT_CYCLE
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
        storage.set_str(key, nxt)
        await cb.answer(f"{key}: {nxt}")
        try:
            await cb.message.edit_text("⚙️ Действие за каждый фильтр (тап — следующее):",
                                       reply_markup=acts_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "rules":
        await cb.answer()
        rules = storage.get_rules()
        body = render_rules(rules) if rules else "(не заданы)"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="panel:setrules")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])
        try:
            await cb.message.edit_text(f"📜 <b>Правила</b>\n\n{body}", reply_markup=kb,
                                       disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
    elif action == "setrules":
        panel_state[uid] = "set_rules"
        await cb.answer()
        await bot.send_message(
            cb.message.chat.id,
            "✍️ Пришли текст правил одним сообщением.\n"
            "Ссылку вшивай так: <code>[наш канал](https://t.me/...)</code>")
    elif action == "bots":
        await cb.answer()
        ch = manager.children(uid)
        txt = (f"🤖 <b>Мои боты</b> ({len(ch)})\n"
               "🟢 работает · 🔴 остановлен · ⏹ стоп · 🗑 удалить.\n"
               "Каждый — отдельный бот со своим паролем и базой.")
        try:
            await cb.message.edit_text(txt, reply_markup=bots_keyboard(uid))
        except TelegramBadRequest:
            pass
    elif action == "addbot":
        panel_state[uid] = "add_bot"
        await cb.answer()
        await bot.send_message(cb.message.chat.id,
                               "✍️ Пришли <b>токен</b> нового бота от @BotFather одним сообщением:")
    elif action == "bstop":
        if manager.owns(parts[2], uid):
            manager.stop(parts[2])
            await cb.answer("Остановлен")
        else:
            await cb.answer("Это не твой бот.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=bots_keyboard(uid))
        except TelegramBadRequest:
            pass
    elif action == "bdel":
        if manager.remove(parts[2], owner=uid):
            await cb.answer("Удалён")
        else:
            await cb.answer("Это не твой бот.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=bots_keyboard(uid))
        except TelegramBadRequest:
            pass
    elif action == "log":
        await cb.answer()
        try:
            await cb.message.edit_text(audit_text(20), reply_markup=back_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "binfo":
        bid = parts[2]
        if not manager.owns(bid, uid):
            await cb.answer("Это не твой бот.", show_alert=True)
            return
        await cb.answer()
        s = _child_stats(bid)
        running = "🟢 работает" if manager.alive(bid) else "🔴 остановлен"
        txt = (f"🤖 <b>Бот {bid}</b> — {running}\n"
               f"Банов: {s.get('banned', 0)} | мутов за картинки: {s.get('img_muted', 0)}\n"
               f"Капч: {s.get('challenged', 0)} (прошли {s.get('passed', 0)}, "
               f"завалили {s.get('failed', 0)})\n"
               f"Жалоб: {s.get('reports', 0)} | рейдов: {s.get('raids', 0)} | "
               f"ЧС: {s.get('crises', 0)}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ К ботам", callback_data="panel:bots")]])
        try:
            await cb.message.edit_text(txt, reply_markup=kb)
        except TelegramBadRequest:
            pass
    elif action == "roles":
        await cb.answer()
        txt = ("🎖 <b>Роли и права</b>\n"
               "TG-админы имеют все права по умолчанию. Здесь — наборы прав для "
               "внутренних ролей (выдаются в чате командой /setrole роль).\n"
               "Выбери роль, чтобы включить/выключить её права:")
        try:
            await cb.message.edit_text(txt, reply_markup=roles_keyboard())
        except TelegramBadRequest:
            pass
    elif action in ("role", "rp"):
        role = parts[2] if len(parts) > 2 else ""
        if role not in config.ROLES:
            await cb.answer("Нет такой роли.", show_alert=True)
            return
        if action == "rp" and len(parts) > 3 and parts[3] in dict(PERM_LABELS):
            perm = parts[3]
            set_role_perm(role, perm, perm not in role_perms(role))
            await cb.answer("Переключено")
        else:
            await cb.answer()
        perms = ", ".join(sorted(role_perms(role))) or "(нет прав)"
        keyhint = f" (ключ <code>{esc(role)}</code>)" if role_title(role) != role else ""
        txt = (f"🎖 Роль <b>{esc(role_title(role))}</b>{keyhint}\nТекущие права: {esc(perms)}\n"
               "Жми, чтобы переключить:")
        try:
            await cb.message.edit_text(txt, reply_markup=role_perm_keyboard(role))
        except TelegramBadRequest:
            pass
    elif action == "rrn":
        role = parts[2] if len(parts) > 2 else ""
        if role not in config.ROLES:
            await cb.answer("Нет такой роли.", show_alert=True)
            return
        panel_state[uid] = f"rename_role:{role}"
        await cb.answer()
        await bot.send_message(
            cb.message.chat.id,
            f"✏️ Пришли новое название для ранга {role_label(role)} "
            f"(ключ <code>{esc(role)}</code>).\nСброс к исходному — пришли <code>-</code>.")
    elif action in ("asg", "unrole"):
        if action == "unrole" and len(parts) > 2 and parts[2].lstrip("-").isdigit():
            tgt = int(parts[2])
            storage.set_role(tgt, None)
            storage.set_user_perms(tgt, None)   # снимаем и личный оверрайд прав
            await cb.answer("Роль снята")
        else:
            await cb.answer()
        n = len(storage.roles_all())
        txt = (f"👥 <b>Назначения ролей</b> ({n})\n"
               "Тап по строке — открыть права человека (можно урезать лично).\n"
               "«Выдать роль» — назначить по id. В чате роли раздаёт только владелец "
               "(/setrole роль ответом).")
        try:
            await cb.message.edit_text(txt, reply_markup=asg_keyboard())
        except TelegramBadRequest:
            pass
    elif action in ("uperm", "up", "upreset"):
        if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
            await cb.answer("Нет пользователя.", show_alert=True)
            return
        tgt = int(parts[2])
        role = storage.get_role(tgt)
        if not role:
            await cb.answer("У человека нет роли.", show_alert=True)
            return
        if action == "up" and len(parts) > 3 and parts[3] in dict(PERM_LABELS):
            perm = parts[3]
            cur = effective_perms(tgt)
            cur.discard(perm) if perm in cur else cur.add(perm)
            storage.set_user_perms(tgt, sorted(cur))   # фиксируем как личный набор
            await cb.answer("Переключено")
        elif action == "upreset":
            storage.set_user_perms(tgt, None)
            await cb.answer("Сброшено к правам роли")
        else:
            await cb.answer()
        who = cached_name(tgt)
        perms = ", ".join(sorted(effective_perms(tgt))) or "(нет прав)"
        override = storage.get_user_perms_override(tgt) is not None
        src = "личный набор" if override else f"по роли {role_title(role)}"
        txt = (f"👤 <b>Права: {esc(who)}</b> (<code>{tgt}</code>)\n"
               f"Роль: {role_label(role)}\nИсточник прав: {src}\n"
               f"Текущие права: {esc(perms)}\n"
               "Жми, чтобы переключить лично этому человеку:")
        try:
            await cb.message.edit_text(txt, reply_markup=user_perm_keyboard(tgt))
        except TelegramBadRequest:
            pass
    elif action == "asgnew":
        await cb.answer()
        try:
            await cb.message.edit_text("➕ <b>Выдать роль</b>\nВыбери роль, потом пришлёшь id:",
                                       reply_markup=asg_pick_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "asgrole":
        role = parts[2] if len(parts) > 2 else ""
        if role not in config.ROLES:
            await cb.answer("Нет такой роли.", show_alert=True)
            return
        panel_state[uid] = f"assign_uid:{role}"
        await cb.answer()
        await bot.send_message(cb.message.chat.id,
                               f"✍️ Пришли <b>id пользователя</b>, кому выдать роль «{esc(role)}»:")
    elif action == "owners":
        await cb.answer()
        txt = ("👑 <b>Владельцы</b>\n"
               "Только владелец раздаёт роли/должности (в чате и здесь).\n"
               "Тап по id — снять; можно сделать владельцем себя или добавить по id.")
        try:
            await cb.message.edit_text(txt, reply_markup=owners_keyboard())
        except TelegramBadRequest:
            pass
    elif action in ("ownme", "ownrm"):
        if action == "ownme":
            storage.add_owner(uid)
            await cb.answer("Ты теперь владелец")
        elif len(parts) > 2 and parts[2].lstrip("-").isdigit():
            storage.remove_owner(int(parts[2]))
            await cb.answer("Снят")
        else:
            await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=owners_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "ownadd":
        panel_state[uid] = "add_owner"
        await cb.answer()
        await bot.send_message(cb.message.chat.id, "✍️ Пришли <b>id</b> нового владельца:")
    elif action == "noop":
        await cb.answer()
    elif action == "close":
        panel_state.pop(uid, None)
        await cb.answer("Закрыто")
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
    else:
        await cb.answer()


# ----------------------------------------------------------------- запуск

async def main():
    storage.load()
    stats.update(storage.load_stats())  # восстановить счётчики
    load_reference_hashes()
    load_nsfw_detector()
    if config.GORE_ENABLED:
        gore.load(config.GORE_MODEL)
    if config.DEANON_ENABLED:
        deanon.load(config.DEANON_OCR_LANG)
    dp.message.outer_middleware(PrivacyGate())  # глушит посторонних в личке
    dp.message.outer_middleware(CommandCleanupMiddleware())  # самый внешний: удаляет команду после обработки
    dp.message.outer_middleware(TrackMiddleware())
    dp.message.outer_middleware(ModerationMiddleware())
    dp.edited_message.outer_middleware(ModerationMiddleware())
    asyncio.create_task(janitor())
    asyncio.create_task(crisis_monitor())
    if not IS_CHILD:
        n = manager.start_all()  # поднять дочерних ботов
        if n:
            log.info("Запущено дочерних ботов: %d", n)
        asyncio.create_task(watchdog())  # следить за дочерними
    me = await bot.get_me()
    global bot_self_id, bot_username
    bot_self_id = me.id
    bot_username = me.username
    role = "дочерний" if IS_CHILD else "родительский"
    updates = list(dp.resolve_used_update_types())
    for u in ("chat_join_request", "chat_member"):  # подстраховка: точно подписаны
        if u not in updates:
            updates.append(u)
    log.info("Запущен как @%s (%s). Подписка на апдейты: %s", me.username, role, updates)
    log.info("Гор-детектор: %s", gore.status())
    try:
        await dp.start_polling(bot, allowed_updates=updates)
    finally:
        if not IS_CHILD:
            manager.stop_all()
        storage.save_stats(stats)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")
