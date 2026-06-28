# -*- coding: utf-8 -*-
"""
Антиспам-бот для Telegram-группы.

Кратко:
  • 3-факторная капча на входе (пример / вопрос / углы фигуры); админы пропускаются;
    имена вступающих проверяются на мат/стоп-слова.
  • Картинки в 2 слоя: хеш по базе photo/ + нейросеть NudeNet (18+).
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

from PIL import Image

from aiogram import Bot, BaseMiddleware, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.exceptions import TelegramBadRequest

import config
import storage
import textguard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
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
night_notice: dict[int, datetime] = {}             # троттлинг уведомления ночного режима
nsfw_detector = None
stats = {"challenged": 0, "passed": 0, "failed": 0, "img_muted": 0, "banned": 0}

MUTE = ChatPermissions(can_send_messages=False)
FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
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


def flag(name: str) -> bool:
    """Булева настройка с рантайм-оверрайдом из storage (команды /night и т.п.)."""
    return storage.get_flag(name, getattr(config, name))


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
    global nsfw_detector
    if not config.NSFW_ENABLED:
        return
    try:
        from nudenet import NudeDetector
        nsfw_detector = NudeDetector()
        log.info("NSFW-детектор (NudeNet) загружен. Классы: %s",
                 ", ".join(config.NSFW_BAD_CLASSES))
    except Exception as e:
        log.warning("NSFW-детектор не загрузился (нагота не проверяется): %s", e)


def _nsfw_detect_sync(data: bytes, tmp_path: str):
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        dets = nsfw_detector.detect(tmp_path)
    except Exception as e:
        log.debug("NSFW-детекция не удалась: %s", e)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    bad = set(config.NSFW_BAD_CLASSES)
    hits = [(x["class"], x["score"]) for x in dets
            if x["class"] in bad and x["score"] >= config.NSFW_MIN_SCORE]
    return max(hits, key=lambda t: t[1]) if hits else None


async def nsfw_check(data: bytes, tag: str):
    if nsfw_detector is None:
        return None
    tmp = os.path.join(tempfile.gettempdir(), f"nsfw_{tag}.jpg")
    return await asyncio.to_thread(_nsfw_detect_sync, data, tmp)


# ------------------------------------------------- учёт сообщений и зачистка

class TrackMiddleware(BaseMiddleware):
    """Запоминает id+время сообщений от юзеров (для зачистки спама)."""

    async def __call__(self, handler, event, data):
        msg = event
        if (msg.from_user and not msg.from_user.is_bot
                and msg.chat.type in ("group", "supergroup")):
            buf = recent.setdefault((msg.chat.id, msg.from_user.id), deque(maxlen=200))
            buf.append((msg.message_id, now()))
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
            fcut = n - timedelta(seconds=config.ANTIFLOOD_SECONDS)
            while buf and buf[0] < fcut:
                buf.popleft()
            if not buf:
                flood.pop(k, None)


# ----------------------------------------------------------- наказания

def mod_keyboard(chat_id: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔨 Бан", callback_data=f"mod:ban:{chat_id}:{uid}"),
        InlineKeyboardButton(text="✅ Размут", callback_data=f"mod:unmute:{chat_id}:{uid}"),
    ]])


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


async def ban_user(chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(chat_id, user_id)
        stats["banned"] += 1
        log.info("Забанен %s в чате %s", user_id, chat_id)
    except TelegramBadRequest as e:
        log.warning("Не смог забанить %s (админ? бот не админ?): %s", user_id, e)


async def mute_user(chat_id: int, user_id: int):
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE)
    except TelegramBadRequest as e:
        log.warning("Не смог замутить %s: %s", user_id, e)


async def apply_punishment(message: Message, reason: str, action: str):
    """Удалить сообщение и применить действие: delete | warn | mute | ban."""
    chat_id = message.chat.id
    user = message.from_user
    uid = user.id
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if action == "warn":
        n = storage.add_warn(chat_id, uid)
        if n >= config.WARN_LIMIT:
            storage.reset_warns(chat_id, uid)
            if config.WARN_ACTION == "ban":
                await ban_user(chat_id, uid)
                await report(chat_id, f"🔨 {mention(user)} забанен: лимит предупреждений ({esc(reason)}).")
            else:
                await mute_user(chat_id, uid)
                await report(chat_id, f"🔇 {mention(user)} в муте: лимит предупреждений ({esc(reason)}).",
                             mod_keyboard(chat_id, uid))
        else:
            await report(chat_id, f"⚠️ {mention(user)}: предупреждение {n}/{config.WARN_LIMIT} — {esc(reason)}.")
    elif action == "mute":
        await mute_user(chat_id, uid)
        await report(chat_id, f"🔇 {mention(user)} в муте: {esc(reason)}.", mod_keyboard(chat_id, uid))
    elif action == "ban":
        await ban_user(chat_id, uid)
        await report(chat_id, f"🔨 {mention(user)} забанен: {esc(reason)}.")
    # action == "delete": тихо удаляем, без уведомления (чтобы не флудить)


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


def antiflood_hit(chat_id: int, user_id: int) -> bool:
    buf = flood.setdefault((chat_id, user_id), deque(maxlen=50))
    t = now()
    buf.append(t)
    cut = t - timedelta(seconds=config.ANTIFLOOD_SECONDS)
    while buf and buf[0] < cut:
        buf.popleft()
    if len(buf) > config.ANTIFLOOD_COUNT:
        buf.clear()
        return True
    return False


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
        if not user or user.is_bot or await is_admin(chat_id, user.id):
            return False

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

        # Пересылки.
        if flag("BLOCK_FORWARDS") and (msg.forward_origin is not None or msg.forward_date is not None):
            await apply_punishment(msg, "пересылка сообщений", config.FORWARD_ACTION)
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
        if (flag("BLOCK_LINKS") and not storage.link_allowed(chat_id, user.id)
                and has_link(msg)):
            await apply_punishment(msg, "ссылка/инвайт", config.LINK_ACTION)
            return True

        # Антифлуд.
        if flag("ANTIFLOOD_ENABLED") and antiflood_hit(chat_id, user.id):
            await apply_punishment(msg, "флуд", config.ANTIFLOOD_ACTION)
            return True

        # Мат и стоп-слова.
        text = msg.text or msg.caption or ""
        if text:
            if flag("ANTIMAT_ENABLED") and textguard.has_profanity(text):
                await apply_punishment(msg, "мат", config.TEXT_ACTION)
                return True
            sw = textguard.find_stopword(text, storage.stopwords())
            if sw:
                await apply_punishment(msg, f"стоп-слово «{sw}»", config.TEXT_ACTION)
                return True
        return False


# ---------------------------------------------------------------- капча

def build_questions() -> list[dict]:
    a, b = random.randint(2, 9), random.randint(2, 9)
    q, ans, wrongs = random.choice(COMMONSENSE)
    name, n = random.choice(list(SHAPES.items()))
    return [
        {"q": f"Шаг 1/3. Реши пример:\n<b>{a} + {b} = ?</b>", "answer": str(a + b), "kind": "num"},
        {"q": f"Шаг 2/3. {esc(q)}", "answer": ans, "wrongs": wrongs},
        {"q": f"Шаг 3/3. Сколько углов у <b>{name}</b>? (ответ цифрой)", "answer": str(n), "kind": "num"},
    ]


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
        await asyncio.sleep(config.CAPTCHA_TIMEOUT)
    except asyncio.CancelledError:
        return
    if (chat_id, user_id) in pending:
        stats["failed"] += 1
        await ban_user(chat_id, user_id)
        await cleanup(chat_id, user_id)


async def send_welcome(chat_id: int, user):
    if not flag("WELCOME_ENABLED"):
        return
    kb = None
    if config.WELCOME_BUTTONS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=b[0], url=b[1])] for b in config.WELCOME_BUTTONS
        ])
    try:
        await bot.send_message(chat_id, f"{mention(user)}, {esc(config.WELCOME_TEXT)}", reply_markup=kb)
    except TelegramBadRequest:
        pass


async def challenge(chat_id: int, user) -> None:
    if user.is_bot:
        return
    key = (chat_id, user.id)
    if key in pending:
        return
    pending[key] = {"steps": None, "idx": 0, "msg_id": None, "task": None}

    try:
        if await is_admin(chat_id, user.id):
            pending.pop(key, None)
            return

        # Стоп-слова/мат в имени вступающего.
        if flag("CHECK_JOIN_NAMES"):
            bad = textguard.is_bad_name(
                f"{user.full_name or ''} {user.username or ''}", storage.stopwords())
            if bad:
                pending.pop(key, None)
                await ban_user(chat_id, user.id)
                await report(chat_id, f"🚫 {mention(user)} забанен на входе: {esc(bad)}.")
                return

        await bot.restrict_chat_member(chat_id, user.id, permissions=MUTE)

        steps = build_questions()
        first = steps[0]
        text = (
            f"👋 {mention(user)}, добро пожаловать!\n"
            f"Пройди проверку из <b>3 заданий</b> за "
            f"<b>{config.CAPTCHA_TIMEOUT} сек</b>. Ошибка или тишина — бан.\n\n"
            f"{first['q']}"
        )
        sent = await bot.send_message(chat_id, text, reply_markup=captcha_markup(0, options_for(first)))
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


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_joined(event: ChatMemberUpdated):
    await challenge(event.chat.id, event.new_chat_member.user)


@dp.message(F.new_chat_members)
async def on_new_members(message: Message):
    if config.DELETE_JOIN_MESSAGE or flag("DELETE_SERVICE_MESSAGES"):
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
        await cleanup(chat_id, user_id)
        return

    new_idx = idx + 1
    if new_idx >= len(state["steps"]):
        await cb.answer("Проверка пройдена ✅")
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=FULL)
        except TelegramBadRequest as e:
            log.warning("Не смог снять мут с %s: %s", user_id, e)
        await cleanup(chat_id, user_id)
        stats["passed"] += 1
        await send_welcome(chat_id, cb.from_user)
        log.info("Юзер %s прошёл капчу — размучен.", user_id)
        return

    state["idx"] = new_idx
    nstep = state["steps"][new_idx]
    text = f"✅ Верно!\n\n{mention(cb.from_user)}, {nstep['q']}"
    try:
        await cb.message.edit_text(text, reply_markup=captcha_markup(new_idx, options_for(nstep)))
    except TelegramBadRequest:
        pass
    await cb.answer("Верно ✅")


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
    log.info("МУТ %s — %s, удалено %d сообщ.", user_id, reason, deleted)


@dp.message(F.photo | F.sticker | F.document)
async def on_media(message: Message):
    if not message.from_user:
        return
    if await is_admin(message.chat.id, message.from_user.id):
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


@dp.callback_query(F.data.startswith("mod:"))
async def on_moderation(cb: CallbackQuery):
    try:
        _, action, gid_str, uid_str = cb.data.split(":")
        gid, uid = int(gid_str), int(uid_str)
    except ValueError:
        await cb.answer()
        return

    if not await is_admin(gid, cb.from_user.id):
        await cb.answer("Решать может только админ.", show_alert=True)
        return

    admin = esc(cb.from_user.full_name)
    if action == "ban":
        await ban_user(gid, uid)
        await cb.answer("Забанен")
        try:
            await cb.message.edit_text(f"🔨 Пользователь забанен. Решение: {admin}.")
        except TelegramBadRequest:
            pass
    elif action == "unmute":
        try:
            await bot.restrict_chat_member(gid, uid, permissions=FULL)
        except TelegramBadRequest as e:
            log.warning("Не смог размутить %s: %s", uid, e)
        await cb.answer("Размучен")
        try:
            await cb.message.edit_text(f"✅ Пользователь размучен. Решение: {admin}.")
        except TelegramBadRequest:
            pass
    else:
        await cb.answer()


# ---------------------------------------------------------------- команды

async def _admin_only(message: Message) -> bool:
    return bool(message.from_user and await is_admin(message.chat.id, message.from_user.id))


def _target_id(message: Message):
    r = message.reply_to_message
    if r and r.from_user:
        return r.from_user.id
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lstrip("-").isdigit():
        return int(parts[1])
    return None


@dp.message(Command("spam"))
async def cmd_spam(message: Message):
    if not await _admin_only(message):
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
    if not await _admin_only(message):
        return
    load_reference_hashes()
    await message.answer(f"🔄 База перезагружена: {len(ref_hashes)} картинок.")


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя или укажи его id.")
        return
    await ban_user(message.chat.id, uid)
    await message.answer("🔨 Забанен.")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя или укажи его id.")
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await message.answer("✅ Разбанен.")
    except TelegramBadRequest as e:
        await message.answer(f"Не вышло: {e}")


@dp.message(Command("mute"))
async def cmd_mute(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя.")
        return
    await mute_user(message.chat.id, uid)
    await message.answer("🔇 В муте.", reply_markup=mod_keyboard(message.chat.id, uid))


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя.")
        return
    try:
        await bot.restrict_chat_member(message.chat.id, uid, permissions=FULL)
        await message.answer("✅ Размучен.")
    except TelegramBadRequest as e:
        await message.answer(f"Не вышло: {e}")


@dp.message(Command("warn"))
async def cmd_warn(message: Message):
    if not await _admin_only(message):
        return
    r = message.reply_to_message
    if not r or not r.from_user:
        await message.answer("Ответь командой на сообщение нарушителя.")
        return
    uid = r.from_user.id
    n = storage.add_warn(message.chat.id, uid)
    if n >= config.WARN_LIMIT:
        storage.reset_warns(message.chat.id, uid)
        if config.WARN_ACTION == "ban":
            await ban_user(message.chat.id, uid)
            await message.answer(f"🔨 {mention(r.from_user)} забанен (лимит предупреждений).")
        else:
            await mute_user(message.chat.id, uid)
            await message.answer(f"🔇 {mention(r.from_user)} в муте (лимит предупреждений).")
    else:
        await message.answer(f"⚠️ {mention(r.from_user)}: предупреждение {n}/{config.WARN_LIMIT}.")


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя.")
        return
    storage.reset_warns(message.chat.id, uid)
    await message.answer("✅ Предупреждения сняты.")


@dp.message(Command("whitelist"))
async def cmd_whitelist(message: Message):
    if not await _admin_only(message):
        return
    uid = _target_id(message)
    if uid is None:
        await message.answer("Ответь командой на пользователя, чтобы разрешить ему ссылки.")
        return
    if storage.allow_link(message.chat.id, uid):
        await message.answer("✅ Пользователю разрешены ссылки.")
    else:
        storage.disallow_link(message.chat.id, uid)
        await message.answer("🚫 Разрешение на ссылки снято.")


@dp.message(Command("addword"))
async def cmd_addword(message: Message):
    if not await _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    word = arg[1].strip() if len(arg) > 1 else ((message.reply_to_message.text or "").strip()
                                                 if message.reply_to_message else "")
    if not word:
        await message.answer("Использование: /addword слово (или ответом на сообщение).")
        return
    if storage.add_stopword(word):
        await message.answer(f"✅ Добавлено стоп-слово. Всего: {len(storage.stopwords())}.")
    else:
        await message.answer("Такое стоп-слово уже есть.")


@dp.message(Command("delword"))
async def cmd_delword(message: Message):
    if not await _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await message.answer("Использование: /delword слово")
        return
    if storage.del_stopword(arg[1].strip()):
        await message.answer(f"✅ Удалено. Осталось: {len(storage.stopwords())}.")
    else:
        await message.answer("Такого стоп-слова нет.")


@dp.message(Command("words"))
async def cmd_words(message: Message):
    if not await _admin_only(message):
        return
    words = storage.stopwords()
    if not words:
        await message.answer("Список стоп-слов пуст.")
    else:
        await message.answer("📋 Стоп-слова:\n" + ", ".join(esc(w) for w in words))


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
    if not await _admin_only(message):
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
    if not await _admin_only(message):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"Тихий режим: {'вкл' if flag('QUIET_MODE') else 'выкл'}. /quiet on|off")
        return
    storage.set_flag("QUIET_MODE", v)
    await message.answer(f"🤫 Тихий режим: {'включён' if v else 'выключен'}.")


@dp.message(Command("antimat"))
async def cmd_antimat(message: Message):
    if not await _admin_only(message):
        return
    v = _parse_onoff(message)
    if v is None:
        await message.answer(f"Антимат: {'вкл' if flag('ANTIMAT_ENABLED') else 'выкл'}. /antimat on|off")
        return
    storage.set_flag("ANTIMAT_ENABLED", v)
    await message.answer(f"🤬 Антимат: {'включён' if v else 'выключен'}.")


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not await _admin_only(message):
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
        f"NudeNet: {'вкл' if nsfw_detector else 'выкл'}"
    )


def stats_text() -> str:
    return (
        "📊 <b>Статистика</b>\n"
        f"Выдано капч: {stats['challenged']} | прошли: {stats['passed']} | "
        f"завалили: {stats['failed']}\n"
        f"Мутов за картинки: {stats['img_muted']} | банов всего: {stats['banned']}\n"
        f"Эталонов: {len(ref_hashes)} | стоп-слов: {len(storage.stopwords())}\n"
        f"Сейчас на капче: {sum(1 for v in pending.values() if v.get('steps'))}"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _admin_only(message):
        return
    await message.answer(stats_text())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not await _admin_only(message):
        return
    await message.answer(
        "🛡 <b>Команды админов</b>\n"
        "Модерация: /ban /unban /mute /unmute (ответом или с id)\n"
        "/warn /unwarn — предупреждения | /whitelist — разрешить ссылки\n"
        "База спама: /spam (ответом на картинку) /reload\n"
        "Стоп-слова: /addword /delword /words\n"
        "Режимы: /night on|off /quiet on|off /antimat on|off\n"
        "Инфо: /settings /stats /ping"
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("pong ✅ бот живой")


@dp.edited_message()
async def on_edited(message: Message):
    # Модерация уже отработала в middleware; здесь ничего не делаем.
    return


# --------------------------------- удаление команд админов после выполнения

class CommandCleanupMiddleware(BaseMiddleware):
    """После обработки удаляет в группе сообщение-команду админа (чистый чат)."""

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        msg = event
        try:
            if (flag("DELETE_ADMIN_COMMANDS") and getattr(msg, "text", None)
                    and msg.text.startswith("/")
                    and msg.chat.type in ("group", "supergroup")
                    and msg.from_user and await is_admin(msg.chat.id, msg.from_user.id)):
                await bot.delete_message(msg.chat.id, msg.message_id)
        except TelegramBadRequest:
            pass
        return result


# ----------------------------------------- админ-панель в личке (пароль)

panel_auth: set[int] = set()        # кто прошёл пароль (сбрасывается при рестарте)
panel_state: dict[int, str] = {}    # ожидание ввода (например, нового стоп-слова)

PANEL_TEXT = ("🛠 <b>Панель управления</b>\n"
              "Зелёная галочка — функция включена. Жми, чтобы переключить.")

PANEL_FLAGS = [
    ("ANTIMAT_ENABLED", "Антимат"),
    ("BLOCK_LINKS", "Ссылки"),
    ("ALLOW_MENTIONS", "Упоминания"),
    ("BLOCK_FORWARDS", "Пересылки"),
    ("BLOCK_CHANNEL_MESSAGES", "Каналы"),
    ("BLOCK_APK", ".apk"),
    ("BLOCK_PREMIUM_EMOJI", "Прем.эмодзи"),
    ("ANTIFLOOD_ENABLED", "Антифлуд"),
    ("CHECK_JOIN_NAMES", "Имена"),
    ("WELCOME_ENABLED", "Приветствие"),
    ("NIGHT_MODE", "Ночной режим"),
    ("QUIET_MODE", "Тихий режим"),
    ("DELETE_SERVICE_MESSAGES", "Чистка сервиса"),
    ("DELETE_ADMIN_COMMANDS", "Чистка команд"),
]


def panel_keyboard() -> InlineKeyboardMarkup:
    rows, buf = [], []
    for key, label in PANEL_FLAGS:
        mark = "✅" if flag(key) else "❌"
        buf.append(InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"panel:t:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="📊 Статистика", callback_data="panel:stats"),
                 InlineKeyboardButton(text="📋 Стоп-слова", callback_data="panel:words")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить базу", callback_data="panel:reload"),
                 InlineKeyboardButton(text="❌ Закрыть", callback_data="panel:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")]])


def words_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"❌ {w[:24]}", callback_data=f"panel:dw:{i}")]
            for i, w in enumerate(storage.stopwords())]
    rows.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data="panel:addword")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def open_panel(chat_id: int):
    await bot.send_message(chat_id, PANEL_TEXT, reply_markup=panel_keyboard())


@dp.message(Command("admin", "start"), F.chat.type == "private")
async def panel_entry(message: Message):
    uid = message.from_user.id
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
    if uid not in panel_auth:
        if message.text and message.text.strip() == config.PANEL_PASSWORD:
            panel_auth.add(uid)
            panel_state.pop(uid, None)
            await message.answer("✅ Доступ открыт.")
            await open_panel(message.chat.id)
        else:
            await message.answer("🔒 Неверный пароль. Попробуй ещё раз:")
        return
    if panel_state.get(uid) == "add_word" and message.text:
        panel_state.pop(uid, None)
        w = message.text.strip()
        if w.startswith("/"):
            await message.answer("Отменено.")
        elif storage.add_stopword(w):
            await message.answer(f"✅ Добавлено стоп-слово «{esc(w)}».")
        else:
            await message.answer("Такое стоп-слово уже есть.")
        await open_panel(message.chat.id)
        return
    await open_panel(message.chat.id)


@dp.callback_query(F.data.startswith("panel:"))
async def panel_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in panel_auth:
        await cb.answer("Нет доступа. Открой панель командой /admin в личке.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "t":
        key = parts[2]
        storage.set_flag(key, not flag(key))
        await cb.answer("Переключено")
        try:
            await cb.message.edit_text(PANEL_TEXT, reply_markup=panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "back":
        await cb.answer()
        try:
            await cb.message.edit_text(PANEL_TEXT, reply_markup=panel_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "stats":
        await cb.answer()
        try:
            await cb.message.edit_text(stats_text(), reply_markup=back_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "words":
        await cb.answer()
        n = len(storage.stopwords())
        try:
            await cb.message.edit_text(
                f"📋 Стоп-слова ({n}). Нажми на слово, чтобы удалить:",
                reply_markup=words_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "addword":
        panel_state[uid] = "add_word"
        await cb.answer()
        await bot.send_message(cb.message.chat.id, "✍️ Напиши новое стоп-слово одним сообщением:")
    elif action == "dw":
        words = storage.stopwords()
        i = int(parts[2])
        if 0 <= i < len(words):
            storage.del_stopword(words[i])
        await cb.answer("Удалено")
        n = len(storage.stopwords())
        try:
            await cb.message.edit_text(
                f"📋 Стоп-слова ({n}). Нажми на слово, чтобы удалить:",
                reply_markup=words_keyboard())
        except TelegramBadRequest:
            pass
    elif action == "reload":
        load_reference_hashes()
        await cb.answer(f"База обновлена: {len(ref_hashes)} картинок.", show_alert=True)
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
    load_reference_hashes()
    load_nsfw_detector()
    dp.message.outer_middleware(CommandCleanupMiddleware())  # самый внешний: удаляет команду после обработки
    dp.message.outer_middleware(TrackMiddleware())
    dp.message.outer_middleware(ModerationMiddleware())
    dp.edited_message.outer_middleware(ModerationMiddleware())
    asyncio.create_task(janitor())
    me = await bot.get_me()
    log.info("Запущен как @%s. Жду новичков…", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")
