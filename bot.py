# -*- coding: utf-8 -*-
"""
Антиспам-бот для Telegram-группы.

Что делает:
  1. Новичок зашёл -> мгновенно мут и 3-факторная капча по шагам:
       Шаг 1 — математический пример (A + B);
       Шаг 2 — первая буква русского алфавита;
       Шаг 3 — сколько углов у фигуры (ответ цифрой).
     Ошибка на любом шаге или тишина за CAPTCHA_TIMEOUT секунд -> бан.
  2. Любое присланное фото сверяется с эталонами из папки photo/. Похоже на
     известный спам -> бот удаляет сообщение, выдаёт МУТ НАВСЕГДА и постит
     модерацию с кнопками [Бан] / [Размут] для админа.

Требования: бот — АДМИН группы с правами
"Блокировка пользователей" (ban), "Ограничение пользователей" (restrict)
и "Удаление сообщений" (delete).
"""

import asyncio
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
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
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

# Нейросеть-детектор 18+ (NudeNet). Создаётся при старте, если NSFW_ENABLED.
nsfw_detector = None

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


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def mention(user) -> str:
    name = (user.full_name or "пользователь").strip() or "пользователь"
    return f'<a href="tg://user?id={user.id}">{name}</a>'


# ---------------------------------------------------------------- картинки

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
    """Загрузить эталоны из папки PHOTO_DIR (вызывается при старте)."""
    ref_hashes.clear()
    base = config.PHOTO_DIR
    if not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), base)
    if not os.path.isdir(base):
        log.warning("Папка с эталонами не найдена: %s", base)
        return
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
    for name in sorted(os.listdir(base)):
        if not name.lower().endswith(exts):
            continue
        path = os.path.join(base, name)
        try:
            with Image.open(path) as img:
                ref_hashes.append((name, dhash(img)))
        except Exception as e:
            log.warning("Эталон %s не загрузился: %s", name, e)
    log.info("Загружено эталонных картинок: %d (%s)",
             len(ref_hashes), ", ".join(n for n, _ in ref_hashes) or "—")


def best_match(h: int) -> tuple[str, int, float] | None:
    """Ближайший эталон: (имя, расстояние, процент_похожести). None если эталонов нет."""
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
    """Синхронная детекция (вызывается в отдельном потоке). NudeNet читает с диска."""
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


# ---------------------------------------------------------------- капча

def build_questions() -> list[dict]:
    """Три задания по порядку. answer — строка, с ней сравниваем нажатую кнопку."""
    a, b = random.randint(2, 9), random.randint(2, 9)
    name, n = random.choice(list(SHAPES.items()))
    return [
        {"q": f"Шаг 1/3. Реши пример:\n<b>{a} + {b} = ?</b>",
         "answer": str(a + b), "kind": "num"},
        {"q": "Шаг 2/3. Назови <b>первую букву</b> русского алфавита:",
         "answer": "А", "kind": "letter"},
        {"q": f"Шаг 3/3. Сколько углов у <b>{name}</b>? (ответ цифрой)",
         "answer": str(n), "kind": "num"},
    ]


def options_for(step: dict) -> list[str]:
    """4 варианта-кнопки для шага (правильный + 3 неверных), перемешанные."""
    ans = step["answer"]
    if step["kind"] == "num":
        n = int(ans)
        opts = {n}
        while len(opts) < 4:
            cand = n + random.randint(-3, 3)
            if cand >= 1:
                opts.add(cand)
        result = [str(x) for x in opts]
    else:
        pool = ["Б", "В", "Г", "Д", "Е", "О", "М", "Я"]
        random.shuffle(pool)
        result = [ans] + pool[:3]
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
        await ban_user(chat_id, user_id)
        await cleanup(chat_id, user_id)


async def challenge(chat_id: int, user) -> None:
    """Замутить новичка и выдать 3-факторную капчу. Идемпотентно."""
    # Боты, добавленные админом, капчу пройти не могут — не трогаем.
    if user.is_bot:
        return

    key = (chat_id, user.id)
    # Резервируем ключ СИНХРОННО (до любого await), иначе два события входа
    # (chat_member + new_chat_members) проходят проверку одновременно -> 2 капчи.
    if key in pending:
        return  # капча уже висит / уже резервируется — не дублируем
    pending[key] = {"steps": None, "idx": 0, "msg_id": None, "task": None}

    # Мут до прохождения капчи.
    try:
        await bot.restrict_chat_member(chat_id, user.id, permissions=MUTE)
    except TelegramBadRequest as e:
        log.warning("Не смог замутить %s (бот не админ?): %s", user.id, e)
        pending.pop(key, None)  # снять резерв — пусть попробует снова
        return

    # Выдаём 3-факторную капчу, начиная с шага 1.
    steps = build_questions()
    first = steps[0]
    text = (
        f"👋 {mention(user)}, добро пожаловать!\n"
        f"Пройди проверку из <b>3 заданий</b> за "
        f"<b>{config.CAPTCHA_TIMEOUT} сек</b>. Ошибка или тишина — бан.\n\n"
        f"{first['q']}"
    )
    kb = captcha_markup(0, options_for(first))
    sent = await bot.send_message(chat_id, text, reply_markup=kb)

    task = asyncio.create_task(captcha_timeout(chat_id, user.id))
    pending[key] = {
        "steps": steps,
        "idx": 0,
        "msg_id": sent.message_id,
        "task": task,
    }
    log.info("Новичок %s (%s) — капча выдана", user.id, user.full_name)


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
    if not state:
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
        await ban_user(chat_id, user_id)
        await cleanup(chat_id, user_id)
        log.info("Забанен %s — ошибка на шаге %s капчи", user_id, idx + 1)
        return

    new_idx = idx + 1
    if new_idx >= len(state["steps"]):
        # Все 3 фактора пройдены -> полный размут.
        await cb.answer("Проверка пройдена ✅")
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=FULL)
        except TelegramBadRequest as e:
            log.warning("Не смог снять мут с %s: %s", user_id, e)
        await cleanup(chat_id, user_id)
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
        return message.document if mt.startswith("image/") else None
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

    # Мут до решения админа.
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE)
    except TelegramBadRequest as e:
        log.warning("Не смог замутить %s: %s", user_id, e)

    # Удаляем весь спам юзера за PURGE_WINDOW_SECONDS (включая связку),
    # затем добиваем остатки альбома, которые подъедут чуть позже.
    deleted = await purge_recent(chat_id, user_id)
    asyncio.create_task(delayed_purge(chat_id, user_id, delay=3.0))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔨 Бан", callback_data=f"mod:ban:{user_id}"),
        InlineKeyboardButton(text="✅ Размут", callback_data=f"mod:unmute:{user_id}"),
    ]])
    mins = config.PURGE_WINDOW_SECONDS // 60
    text = (
        f"🚫 {mention(message.from_user)}: {reason}.\n"
        f"Удалено сообщений за {mins} мин: <b>{deleted}</b>. Выдан <b>мут</b>.\n\n"
        f"Админ, проверь лог нарушения (Управление группой → "
        f"Недавние действия) и реши:"
    )
    await bot.send_message(chat_id, text, reply_markup=kb)
    log.info("МУТ %s — %s, удалено %d сообщ.", user_id, reason, deleted)


@dp.message(F.photo | F.sticker | F.document)
async def on_media(message: Message):
    """Проверяем картинку: 1) хеш по базе, 2) нейросеть 18+. Совпало -> нарушение."""
    if not message.from_user:
        return

    file_obj = pick_image_file(message)
    if file_obj is None:
        return

    try:
        buf = await bot.download(file_obj)
        data = buf.read()
    except Exception as e:
        log.warning("Не смог скачать изображение: %s", e)
        return

    uid = message.from_user.id

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

    log.info("Картинка от %s чистая", uid)


@dp.callback_query(F.data.startswith("mod:"))
async def on_moderation(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    try:
        _, action, uid_str = cb.data.split(":", 2)
        uid = int(uid_str)
    except ValueError:
        await cb.answer()
        return

    # Кнопки только для админов.
    try:
        member = await bot.get_chat_member(chat_id, cb.from_user.id)
        is_admin = member.status in (
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR
        )
    except TelegramBadRequest:
        is_admin = False
    if not is_admin:
        await cb.answer("Решать может только админ.", show_alert=True)
        return

    admin = cb.from_user.full_name
    if action == "ban":
        await ban_user(chat_id, uid)
        await cb.answer("Забанен")
        try:
            await cb.message.edit_text(f"🔨 Пользователь забанен. Решение: {admin}.")
        except TelegramBadRequest:
            pass
    elif action == "unmute":
        try:
            await bot.restrict_chat_member(chat_id, uid, permissions=FULL)
        except TelegramBadRequest as e:
            log.warning("Не смог размутить %s: %s", uid, e)
        await cb.answer("Размучен")
        try:
            await cb.message.edit_text(f"✅ Пользователь размучен. Решение: {admin}.")
        except TelegramBadRequest:
            pass
    else:
        await cb.answer()


@dp.message(F.text == "/ping")
async def ping(message: Message):
    await message.reply("pong ✅ бот живой")


async def main():
    load_reference_hashes()
    load_nsfw_detector()
    dp.message.outer_middleware(TrackMiddleware())  # учёт сообщений для зачистки
    me = await bot.get_me()
    log.info("Запущен как @%s. Жду новичков…", me.username)
    # ВАЖНО: chat_member не входит в подписку по умолчанию — без него бот
    # не видит тех, кто вступил по ссылке (большие группы). Просим явно.
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")
