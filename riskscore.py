# -*- coding: utf-8 -*-
"""
Риск-движок: оценивает профиль пользователя и выдаёт числовой «риск-скор»
(0..100+) со списком причин. Чем выше — тем больше похоже на спам-бота
(«очередной акк из Индонезии с иностранным именем», случайный юзернейм,
свежая регистрация, без фото и т.п.).

Всё здесь — ЧИСТЫЕ функции без сети и без aiogram: их гоняют юниты в tests.py,
а bot.py лишь подставляет сигналы (наличие аватара, датацентр), которые
достаёт через Bot API. Так движок легко тестировать и настраивать.

ВАЖНО (честно): это ЭВРИСТИКА. Она НЕ должна банить сама по себе на грани —
поэтому в боте высокий порог на жёсткое действие, а основная реакция это
«наблюдение» (probation) + мут на подозрительное поведение, а не мгновенный бан.
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Веса сигналов. Сумма сработавших даёт риск-скор. Значения подобраны так,
# чтобы обычный русскоязычный пользователь с ником/аватаром набирал ~0, а
# «свежий бот с иностранным именем без фото и случайным юзернеймом» — много.
# Переопределяются из config.RISK_WEIGHTS (мерджится поверх этих).
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "no_photo": 15,          # нет аватара
    "no_username": 8,        # нет @username
    "random_username": 16,   # юзернейм вида Name480213 / kldfjghqwe
    "new_account": 18,       # свежая регистрация (по величине user_id)
    "script_latin": 6,       # имя латиницей (частое и у реальных — вес малый)
    "script_arabic": 20,     # арабица/иврит в русскоязычном чате
    "script_cjk": 18,        # китайский/японский/корейский
    "script_indic": 16,      # деванагари/тай/бенгали и пр. (частый регион ботов)
    "script_symbols": 22,    # имя из значков/эмодзи/невидимок
    "name_adlike": 22,       # в имени ссылка/@/телефон/«продам» — реклама
    "emoji_in_name": 5,      # эмодзи в имени
    "dc_flagged": 15,        # датацентр аватара из «чёрного» списка (DC5 и пр.)
    "premium": -12,          # Telegram Premium — реже спам (снижает риск)
    "has_last_name": -4,     # есть фамилия — чуть человечнее
}

# «Рекламные» маркеры в имени/нике (в нижнем регистре, без учёта пробелов).
_ADLIKE = re.compile(
    r"(https?://|www\.|t\.me/|@[a-z0-9_]{3,}|"
    r"\+7\d{6,}|8\d{9,}|"
    r"прода|куплю|скидк|казино|ставк|крипт|инвест|зараб|18\+|xxx|"
    r"onlyfans|promo|bonus|casino|bet\b)",
    re.IGNORECASE,
)

# Юзернейм похож на автогенерированный: буквы + длинный хвост цифр,
# или почти нет гласных (набор согласных), или явный «бот-паттерн».
_UNAME_DIGITS_TAIL = re.compile(r"^[a-z][a-z_]*\d{4,}$", re.IGNORECASE)
_UNAME_MIXED_DIGITS = re.compile(r"\d{5,}")
_VOWELS = set("aeiouyаеёиоуыэюя")


def _letters(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalpha())


def dominant_script(text: str) -> str:
    """Определить преобладающую письменность имени.

    Возвращает одно из: 'cyrillic', 'latin', 'arabic', 'cjk', 'indic',
    'symbols' (значки/эмодзи, букв нет) или 'other'.
    """
    counts: dict[str, int] = {}
    letters = 0
    for ch in text or "":
        if ch.isspace():
            continue
        if not ch.isalpha():
            continue
        letters += 1
        try:
            name = unicodedata.name(ch)
        except ValueError:
            counts["other"] = counts.get("other", 0) + 1
            continue
        if "CYRILLIC" in name:
            k = "cyrillic"
        elif "LATIN" in name:
            k = "latin"
        elif "ARABIC" in name or "HEBREW" in name:
            k = "arabic"
        elif ("CJK" in name or "HIRAGANA" in name or "KATAKANA" in name
              or "HANGUL" in name):
            k = "cjk"
        elif ("DEVANAGARI" in name or "THAI" in name or "BENGALI" in name
              or "TAMIL" in name or "TELUGU" in name or "GUJARATI" in name
              or "KANNADA" in name or "MALAYALAM" in name or "SINHALA" in name
              or "MYANMAR" in name or "KHMER" in name or "LAO" in name):
            k = "indic"
        else:
            k = "other"
        counts[k] = counts.get(k, 0) + 1
    if letters == 0:
        # Букв нет вовсе: только эмодзи/значки/невидимки (или пусто).
        stripped = "".join((text or "").split())
        return "symbols" if stripped else "other"
    return max(counts, key=counts.get)


def has_emoji(text: str) -> bool:
    """Есть ли в строке эмодзи/пиктограмма (грубо — по категории символа)."""
    for ch in text or "":
        if unicodedata.category(ch) in ("So", "Sk"):
            return True
        if ord(ch) >= 0x1F000:
            return True
    return False


def is_random_username(username: str | None) -> bool:
    """Похож ли @username на автосгенерированный ботом."""
    if not username:
        return False
    u = username.strip().lstrip("@")
    if len(u) < 5:
        return False
    if _UNAME_DIGITS_TAIL.match(u) or _UNAME_MIXED_DIGITS.search(u):
        return True
    # Почти нет гласных при заметной длине — набор согласных «kldfjghqw».
    letters = [c for c in u.lower() if c.isalpha()]
    if len(letters) >= 7:
        vowels = sum(1 for c in letters if c in _VOWELS)
        if vowels / len(letters) < 0.18:
            return True
    return False


def is_fresh_account(user_id: int | None, threshold: int) -> bool:
    """Свежая ли регистрация. user_id в Telegram растёт со временем создания
    аккаунта, поэтому id выше порога — «молодой» аккаунт. threshold<=0 = выкл."""
    if not user_id or threshold <= 0:
        return False
    return int(user_id) >= threshold


def score_profile(
    *,
    first_name: str = "",
    last_name: str = "",
    username: str | None = None,
    user_id: int | None = None,
    is_premium: bool = False,
    has_photo: bool | None = None,
    dc: int | None = None,
    dc_flagged: bool = False,
    fresh_id_threshold: int = 0,
    weights: dict | None = None,
) -> tuple[int, list[str]]:
    """Оценить профиль. Возвращает (скор, причины).

    Аргументы — уже добытые сигналы (bot.py тянет их из Bot API):
      has_photo   — есть ли аватар (None = неизвестно, сигнал не учитываем);
      dc_flagged  — датацентр аватара в «чёрном» списке;
      fresh_id_threshold — порог «свежести» по user_id (0 = не учитывать).
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    score = 0
    reasons: list[str] = []

    def add(key: str, label: str):
        nonlocal score
        val = w.get(key, 0)
        if val:
            score += val
            reasons.append(f"{label} ({val:+d})")

    full = f"{first_name or ''} {last_name or ''}".strip()

    if has_photo is False:
        add("no_photo", "нет аватара")
    if not (username or "").strip():
        add("no_username", "нет @username")
    elif is_random_username(username):
        add("random_username", "случайный @username")

    if is_fresh_account(user_id, fresh_id_threshold):
        add("new_account", "свежий аккаунт")

    script = dominant_script(full)
    if script == "latin":
        add("script_latin", "имя латиницей")
    elif script == "arabic":
        add("script_arabic", "имя арабицей/иврит")
    elif script == "cjk":
        add("script_cjk", "имя CJK")
    elif script == "indic":
        add("script_indic", "имя индик/тай")
    elif script == "symbols":
        add("script_symbols", "имя из значков")

    if _ADLIKE.search(full) or _ADLIKE.search(username or ""):
        add("name_adlike", "реклама/ссылка в имени")
    if has_emoji(full):
        add("emoji_in_name", "эмодзи в имени")

    if dc_flagged:
        add("dc_flagged", f"датацентр DC{dc}")

    if is_premium:
        add("premium", "Telegram Premium")
    if (last_name or "").strip():
        add("has_last_name", "есть фамилия")

    return max(0, score), reasons


def verdict(score: int, watch_threshold: int, ban_threshold: int) -> str:
    """Итог по скору: 'clear' | 'watch' | 'hard'."""
    if ban_threshold and score >= ban_threshold:
        return "hard"
    if watch_threshold and score >= watch_threshold:
        return "watch"
    return "clear"
