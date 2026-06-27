# -*- coding: utf-8 -*-
"""
Антиспам-бот для Telegram-группы.

Что делает:
  1. Новичок зашёл -> мгновенно мут и 3-факторная капча по шагам:
       Шаг 1 — математический пример (A + B);
       Шаг 2 — случайный вопрос на здравый смысл (из пула);
       Шаг 3 — сколько углов у фигуры (ответ цифрой).
     Ошибка на любом шаге или тишина за CAPTCHA_TIMEOUT секунд -> бан.
     Админы группы капчу не проходят (пропускаются).
  2. Любое присланное фото проверяется в 2 слоя:
       а) хеш по базе photo/ (точный/пережатый повтор известного спама);
       б) нейросеть NudeNet (нагота/18+ на любой новой картинке).
     Совпало -> удалить сообщение, мут, вычистить весь спам юзера за
     PURGE_WINDOW_SECONDS (вся «связка»/альбом) и кнопки [Бан]/[Размут] админу.
  3. Админ-команды: /spam (ответом на картинку — в базу + наказать),
     /reload, /stats, /help, /ping.

Требования: бот — АДМИН группы с правами
"Блокировка пользователей" (ban), "Ограничение пользователей" (restrict)
и "Удаление сообщений" (delete).
"""

import asyncio
import html
import io
import logging
import os
import random
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

# Ожидающие капчу: (chat_id, user_id) -> {steps, idx, msg_id, task}.
pending: dict[tuple[int, int], dict] = {}

# Эталонные хеши спам-картинок: список (имя_файла, dhash:int). Грузится при старте.
ref_hashes: list[tuple[str, int]] = []

# Буфер последних сообщений: (chat_id, user_id) -> deque[(msg_id, datetime)].
# Нужен, чтобы при нарушении вычистить весь спам юзера за окно PURGE_WINDOW_SECONDS.
recent: dict[tuple[int, int], deque] = {}

# Антидубль: (chat_id, user_id) -> datetime последнего срабатывания модерации,
# чтобы альбом из N фото не плодил N уведомлений админу.
flagged: dict[tuple[int, int], datetime] = {}

# Кэш админов: chat_id -> (set(user_id), datetime_обновления).
admins_cache: dict[int, tuple[set, datetime]] = {}

# Нейросеть-детектор 18+ (NudeNet). Создаётся при старте, если NSFW_ENABLED.
nsfw_detector = None

# Простая статистика для /stats.
stats = {"challenged": 0, "passed": 0, "failed": 0, "img_muted": 0, "banned": 0}

# Полный мут (ничего нельзя слать) — пока висит капча / на время модерации.
MUTE = ChatPermissions(can_send_messages=False)

# Полный размут (обычные права участника).
FULL = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

# Фигуры для 3-го фактора: название -> число углов.
SHAPES = {
    "треугольника": 3,
    "квадрата": 4,
    "пятиугольника": 5,
    "шестиугольника": 6,
}

# Пул вопросов на здравый смысл для 2-го фактора: (вопрос, ответ, [неверные]).
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

MAX_DOC_BYTES = 10 * 1024 * 1024  # картинки-документы крупнее не анализируем


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def esc(text: str) -> str:
    """Экранировать текст для HTML-разметки (имена юзеров могут содержать < > &)."""
    return html.escape(text or "", quote=False)


def mention(user) -> str:
    name = (user.full_name or "пользователь").strip() or "пользователь"
    return f'<a href="tg://user?id={user.id}">{esc(name)}</a>'


# ----------------------------------------------------------------- админы

async def get_admins(chat_id: int) -> set:
    """ID админов чата с кэшем (TTL), чтобы не дёргать API на каждое сообщение."""
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
    """Difference hash 64 бита: устойчив к ресайзу/пережатию одной картинки."""
    img = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = img.tobytes()  # row-major байты яркости, px[i] -> int 0..255
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
    except Exception as e:  # битый файл / не-картинка / анимация
        log.debug("Не смог распознать изображение: %s", e)
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def load_reference_hashes() -> None:
    """Загрузить эталоны из папки PHOTO_DIR (при старте и по /reload)."""
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
    """Ближайший эталон: (имя, расстояние, процент_похожести). None если базы нет."""
    if not ref_hashes:
        return None
    name, dist = min(((n, hamming(h, rh)) for n, rh in ref_hashes),
                     key=lambda t: t[1])
    percent = (64 - dist) / 64 * 100
    return name, dist, percent


def load_nsfw_detector() -> None:
    """Поднять нейросеть NudeNet (если включена и установлена)."""
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
    """Синхронная детекция (в отдельном потоке). NudeNet читает с диска."""
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
    if not hits:
        return None
    return max(hits, key=lambda t: t[1])  # самый уверенный класс


async def nsfw_check(data: bytes, tag: str):
    """Проверить байты картинки на 18+. Возвращает (класс, уверенность) или None."""
    if nsfw_detector is None:
        return None
    tmp = os.path.join(tempfile.gettempdir(), f"nsfw_{tag}.jpg")
    return await asyncio.to_thread(_nsfw_detect_sync, data, tmp)


# ------------------------------------------------- учёт сообщений и зачистка

class TrackMiddleware(BaseMiddleware):
    """Запоминает id+время каждого сообщения от юзеров (для зачистки спама)."""

    async def __call__(self, handler, event, data):
        msg = event
        if (msg.from_user and not msg.from_user.is_bot
                and msg.chat.type in ("group", "supergroup")):
            key = (msg.chat.id, msg.from_user.id)
            buf = recent.setdefault(key, deque(maxlen=200))
            buf.append((msg.message_id, now()))
        return await handler(event, data)


async def purge_recent(chat_id: int, user_id: int) -> int:
    """Удалить все запомненные сообщения юзера за окно PURGE_WINDOW_SECONDS."""
    buf = recent.get((chat_id, user_id))
    if not buf:
        return 0
    cutoff = now() - timedelta(seconds=config.PURGE_WINDOW_SECONDS)
    ids = [mid for mid, t in buf if t >= cutoff]
    deleted = 0
    for i in range(0, len(ids), 100):  # bulk-удаление пачками по 100
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
            deleted += len(chunk)
        except TelegramBadRequest:
            for mid in chunk:  # фоллбэк поштучно
                try:
                    await bot.delete_message(chat_id, mid)
                    deleted += 1
                except TelegramBadRequest:
                    pass
    recent.pop((chat_id, user_id), None)
    return deleted


async def delayed_purge(chat_id: int, user_id: int, delay: float = 3.0):
    """Повторная зачистка: ловит остатки альбома, пришедшие после нарушения."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    n = await purge_recent(chat_id, user_id)
    if n:
        log.info("Догнал и удалил ещё %d сообщений от %s", n, user_id)


async def janitor():
    """Фоновая уборка: не даём словарям расти бесконечно."""
    while True:
        try:
            await asyncio.sleep(600)  # каждые 10 минут
        except asyncio.CancelledError:
            return
        n = now()
        for k in [k for k, t in list(flagged.items())
                  if (n - t).total_seconds() > 60]:
            flagged.pop(k, None)
        cutoff = n - timedelta(seconds=config.PURGE_WINDOW_SECONDS)
        for k in list(recent.keys()):
            buf = recent.get(k)
            if buf is None:
                continue
            while buf and buf[0][1] < cutoff:
                buf.popleft()
            if not buf:
                recent.pop(k, None)


# ---------------------------------------------------------------- капча

def build_questions() -> list[dict]:
    """Три задания по порядку. answer — строка, с ней сравниваем нажатую кнопку."""
    a, b = random.randint(2, 9), random.randint(2, 9)
    q, ans, wrongs = random.choice(COMMONSENSE)
    name, n = random.choice(list(SHAPES.items()))
    return [
        {"q": f"Шаг 1/3. Реши пример:\n<b>{a} + {b} = ?</b>",
         "answer": str(a + b), "kind": "num"},
        {"q": f"Шаг 2/3. {esc(q)}", "answer": ans, "wrongs": wrongs},
        {"q": f"Шаг 3/3. Сколько углов у <b>{name}</b>? (ответ цифрой)",
         "answer": str(n), "kind": "num"},
    ]


def options_for(step: dict) -> list[str]:
    """4 варианта-кнопки для шага (правильный + 3 неверных), перемешанные."""
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
    row = [
        InlineKeyboardButton(text=o, callback_data=f"cap:{idx}:{o}")
        for o in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def ban_user(chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(chat_id, user_id)
        stats["banned"] += 1
        log.info("Забанен %s в чате %s", user_id, chat_id)
    except TelegramBadRequest as e:
        log.warning("Не смог забанить %s (админ? бот не админ?): %s", user_id, e)


async def cleanup(chat_id: int, user_id: int, *, delete_msg: bool = True):
    """Снять состояние капчи и (опц.) удалить её сообщение."""
    state = pending.pop((chat_id, user_id), None)
    if not state:
        return
    task: asyncio.Task | None = state.get("task")
    if task and not task.done():
        task.cancel()
    if delete_msg and config.DELETE_CAPTCHA_MESSAGE and state.get("msg_id"):
        try:
            await bot.delete_message(chat_id, state["msg_id"])
        except TelegramBadRequest:
            pass


async def captcha_timeout(chat_id: int, user_id: int):
    """Не успел пройти капчу за отведённое время -> бан."""
    try:
        await asyncio.sleep(config.CAPTCHA_TIMEOUT)
    except asyncio.CancelledError:
        return
    if (chat_id, user_id) in pending:
        stats["failed"] += 1
        await ban_user(chat_id, user_id)
        await cleanup(chat_id, user_id)


async def challenge(chat_id: int, user) -> None:
    """Замутить новичка и выдать 3-факторную капчу. Идемпотентно."""
    if user.is_bot:
        return  # боты, добавленные админом, капчу пройти не могут

    key = (chat_id, user.id)
    # Резервируем ключ СИНХРОННО (до любого await), иначе два события входа
    # (chat_member + new_chat_members) проходят проверку одновременно -> 2 капчи.
    if key in pending:
        return
    pending[key] = {"steps": None, "idx": 0, "msg_id": None, "task": None}

    try:
        if await is_admin(chat_id, user.id):
            pending.pop(key, None)  # админов не проверяем
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
        sent = await bot.send_message(
            chat_id, text, reply_markup=captcha_markup(0, options_for(first))
        )
        task = asyncio.create_task(captcha_timeout(chat_id, user.id))
        pending[key] = {"steps": steps, "idx": 0,
                        "msg_id": sent.message_id, "task": task}
        stats["challenged"] += 1
        log.info("Новичок %s (%s) — капча выдана", user.id, user.full_name)
    except TelegramBadRequest as e:
        log.warning("Не смог выдать капчу %s (бот не админ?): %s", user.id, e)
        pending.pop(key, None)  # снять резерв, чтобы не залип
    except Exception as e:
        log.exception("Ошибка в challenge для %s: %s", user.id, e)
        pending.pop(key, None)


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_joined(event: ChatMemberUpdated):
    """Главный вход: ловит и добавленных, и зашедших по ссылке (большие группы)."""
    await challenge(event.chat.id, event.new_chat_member.user)


@dp.message(F.new_chat_members)
async def on_new_members(message: Message):
    """Подстраховка + чистка служебного сообщения «X вошёл в группу»."""
    if config.DELETE_JOIN_MESSAGE:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    for user in message.new_chat_members:
        await challenge(message.chat.id, user)


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
        log.info("Забанен %s — ошибка на шаге %s капчи", user_id, idx + 1)
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
        log.info("Юзер %s прошёл капчу — размучен.", user_id)
        return

    # Переходим к следующему шагу — редактируем то же сообщение.
    state["idx"] = new_idx
    nstep = state["steps"][new_idx]
    text = f"✅ Верно!\n\n{mention(cb.from_user)}, {nstep['q']}"
    try:
        await cb.message.edit_text(
            text, reply_markup=captcha_markup(new_idx, options_for(nstep))
        )
    except TelegramBadRequest:
        pass
    await cb.answer("Верно ✅")


# ---------------------------------------------------------- анализ картинок

def pick_image_file(message: Message):
    """Вернуть скачиваемый объект-картинку из сообщения или None."""
    if message.photo:
        return message.photo[-1]
    if message.sticker:
        st = message.sticker
        if st.is_animated or st.is_video:
            return None  # анимированные/видео-стикеры не анализируем
        return st
    if message.document:
        mt = message.document.mime_type or ""
        if not mt.startswith("image/"):
            return None
        if (message.document.file_size or 0) > MAX_DOC_BYTES:
            return None  # слишком большой файл не качаем
        return message.document
    return None


async def handle_violation(message: Message, reason: str) -> None:
    """Единый сценарий нарушения: мут + зачистка спама + кнопки админу."""
    chat_id = message.chat.id
    user_id = message.from_user.id
    key = (chat_id, user_id)

    # Антидубль: альбом из N фото не должен слать N уведомлений админу.
    last = flagged.get(key)
    if last and (now() - last).total_seconds() < 30:
        await delayed_purge(chat_id, user_id, delay=3.0)  # добить остатки связки
        return
    flagged[key] = now()

    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE)
    except TelegramBadRequest as e:
        log.warning("Не смог замутить %s: %s", user_id, e)

    # Удаляем весь спам юзера за PURGE_WINDOW_SECONDS (включая связку),
    # затем добиваем остатки альбома, которые подъедут чуть позже.
    deleted = await purge_recent(chat_id, user_id)
    asyncio.create_task(delayed_purge(chat_id, user_id, delay=3.0))
    stats["img_muted"] += 1

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔨 Бан", callback_data=f"mod:ban:{chat_id}:{user_id}"),
        InlineKeyboardButton(text="✅ Размут", callback_data=f"mod:unmute:{chat_id}:{user_id}"),
    ]])
    mins = config.PURGE_WINDOW_SECONDS // 60
    text = (
        f"🚫 {mention(message.from_user)}: {esc(reason)}.\n"
        f"Удалено сообщений за {mins} мин: <b>{deleted}</b>. Выдан <b>мут</b>.\n\n"
        f"Админ, проверь лог нарушения (Управление группой → "
        f"Недавние действия) и реши:"
    )
    target = config.LOG_CHAT_ID or chat_id
    try:
        await bot.send_message(target, text, reply_markup=kb)
    except TelegramBadRequest:
        if target != chat_id:  # лог-канал недоступен — постим в группе
            await bot.send_message(chat_id, text, reply_markup=kb)
    log.info("МУТ %s — %s, удалено %d сообщ.", user_id, reason, deleted)


@dp.message(F.photo | F.sticker | F.document)
async def on_media(message: Message):
    """Проверяем картинку: 1) хеш по базе, 2) нейросеть 18+. Совпало -> нарушение."""
    if not message.from_user:
        return
    if await is_admin(message.chat.id, message.from_user.id):
        return  # картинки админов не трогаем

    file_obj = pick_image_file(message)
    if file_obj is None:
        return

    try:
        buf = await bot.download(file_obj)
        data = buf.read()
    except Exception as e:
        log.warning("Не смог скачать изображение: %s", e)
        return

    # 1) Быстрый слой: точное совпадение с базой спам-картинок.
    h = dhash_from_bytes(data)
    m = best_match(h) if h is not None else None
    if m and m[2] >= config.IMAGE_MATCH_PERCENT:
        name, _, percent = m
        await handle_violation(
            message, f"спам-картинка (похожесть {percent:.0f}% на {name})"
        )
        return

    # 2) Нейросеть: любая нагота/18+ на новой картинке.
    tag = f"{message.chat.id}_{message.message_id}"
    hit = await nsfw_check(data, tag)
    if hit:
        cls, score = hit
        await handle_violation(message, f"18+ контент ({cls}, {score:.0%})")
        return

    log.info("Картинка от %s чистая", message.from_user.id)


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

@dp.message(Command("spam"))
async def cmd_spam(message: Message):
    """Ответом на сообщение с картинкой: добавить её в базу спама и наказать автора."""
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    reply = message.reply_to_message
    if not reply:
        await message.reply("Ответь этой командой на сообщение с картинкой-спамом.")
        return
    file_obj = pick_image_file(reply)
    if file_obj is None:
        await message.reply("В том сообщении нет подходящей картинки.")
        return
    try:
        data = (await bot.download(file_obj)).read()
    except Exception as e:
        await message.reply(f"Не смог скачать картинку: {e}")
        return

    h = dhash_from_bytes(data)
    if h is None:
        await message.reply("Не смог обработать это изображение.")
        return

    fname = f"spam_{reply.message_id}.jpg"
    try:
        with open(os.path.join(photo_dir(), fname), "wb") as f:
            f.write(data)
    except OSError as e:
        log.warning("Не смог сохранить эталон: %s", e)
    ref_hashes.append((fname, h))

    # Наказываем автора исходного сообщения (если он не админ).
    punished = ""
    if reply.from_user and not await is_admin(message.chat.id, reply.from_user.id):
        await handle_violation(reply, "картинка отмечена админом как спам")
        punished = " Автор замучен, его спам вычищен."

    await message.reply(
        f"✅ Добавил в базу спама (всего {len(ref_hashes)}). "
        f"Теперь такие картинки ловлю по хешу.{punished}"
    )


@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    load_reference_hashes()
    await message.reply(f"🔄 База перезагружена: {len(ref_hashes)} картинок.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    await message.reply(
        "📊 <b>Статистика</b>\n"
        f"Выдано капч: {stats['challenged']}\n"
        f"Прошли: {stats['passed']} | завалили/таймаут: {stats['failed']}\n"
        f"Мутов за картинки: {stats['img_muted']}\n"
        f"Банов всего: {stats['banned']}\n"
        f"Эталонов в базе: {len(ref_hashes)}\n"
        f"NudeNet: {'вкл' if nsfw_detector else 'выкл'}\n"
        f"Сейчас на капче: {sum(1 for v in pending.values() if v.get('steps'))}"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    await message.reply(
        "🛡 <b>Команды (только для админов)</b>\n"
        "/spam — ответом на картинку: добавить в базу спама и наказать автора\n"
        "/reload — перечитать папку photo/ с эталонами\n"
        "/stats — статистика\n"
        "/ping — проверка живости"
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.reply("pong ✅ бот живой")


# ----------------------------------------------------------------- запуск

async def main():
    load_reference_hashes()
    load_nsfw_detector()
    dp.message.outer_middleware(TrackMiddleware())  # учёт сообщений для зачистки
    asyncio.create_task(janitor())                  # фоновая уборка памяти
    me = await bot.get_me()
    log.info("Запущен как @%s. Жду новичков…", me.username)
    # ВАЖНО: chat_member не входит в подписку по умолчанию — без него бот
    # не видит тех, кто вступил по ссылке (большие группы). Просим явно.
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")
