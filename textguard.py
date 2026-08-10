# -*- coding: utf-8 -*-
"""
Текстовые фильтры: нормализация подмены символов (гомоглифы), антимат, стоп-слова.

Цель антимата — ловить мат и его обфускации (хyй, х.у.й, пиzдец), НЕ задевая
обычные слова (требовать, хлебать, сухую, команда, рубля).
"""

import re

# Латиница/цифры, визуально подменяющие кириллицу -> кириллица.
HOMOGLYPHS = {
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "z": "з",
    "3": "з", "0": "о", "@": "а", "$": "с",
}
_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠﻿]")
_TRANS = str.maketrans(HOMOGLYPHS)

# Слова, которые внешне задевают корень мата, но чистые.
WHITELIST = {"сухую", "всухую", "посухую", "сухущий"}

# Приставки для корня «еб» (длинные раньше коротких).
_PREF = (r"(?:объ|подъ|разъ|разо|пере|недо|съ|въ|изъ|без|вы|за|на|до|от|об|про|"
         r"раз|при|по|со|из|не|у|о)?")

# Корни мата. ху/пизд — подстрокой; остальные — с границей слова.
_PATTERNS = [
    r"ху[йяеёюи]",
    r"п[иеё]зд",
    r"(?:^|[^а-яё])" + _PREF + r"[её]б",          # ебать/заебал/выеб/ёбнул...
    r"[оа][её]б",                                   # долбоёб/мудоёб (не «погреб»)
    r"(?:^|[^а-яё])бля",                            # бля/блядь
    r"(?:^|[^а-яё])сук[аиоую]",                     # сука/суки (не «барсук»)
    r"(?:^|[^а-яё])муд[аио]",                       # мудак (не «мудрость»)
    r"залуп",
    r"г[ао]ндон",
    r"пид[оа]р|педик|пидр",
    r"дроч",
    r"еблан",
]
_PROF_RE = [re.compile(p) for p in _PATTERNS]

# Узкий набор для «схлопнутого» текста (обфускация пробелами/точками: х у й).
_COLLAPSED_BAD = [
    "хуй", "нахуй", "похуй", "дохуя", "нихуя", "пизд", "блядь",
    "залуп", "гандон", "пидор", "педик", "мудак", "дроч", "еблан",
    "ахуе", "охуе",
]


def normalize(text: str) -> str:
    """Нижний регистр, замена гомоглифов, удаление невидимых символов."""
    if not text:
        return ""
    text = _ZERO_WIDTH.sub("", text.lower())
    return text.translate(_TRANS)


def _words(norm: str) -> list[str]:
    return re.findall(r"[а-яё]+", norm)


def has_profanity(text: str) -> bool:
    norm = normalize(text)
    words = _words(norm)
    for w in words:
        if w in WHITELIST:
            continue
        probe = " " + w  # чтобы (?:^|[^а-яё]) сработал на границе слова
        if any(rx.search(probe) for rx in _PROF_RE):
            return True
    collapsed = "".join(words)  # «х у й» -> «хуй»
    return any(bad in collapsed for bad in _COLLAPSED_BAD)


def _squeeze(s: str) -> str:
    """Схлопнуть повторы букв: «спааам» -> «спам» (любой повтор -> 1)."""
    out = []
    prev = ""
    for ch in s:
        if ch != prev:
            out.append(ch)
            prev = ch
    return "".join(out)


def _levenshtein(a: str, b: str, max_d: int) -> int:
    """Расстояние редактирования с ранним выходом (если > max_d)."""
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            if v < best:
                best = v
        if best > max_d:
            return max_d + 1
        prev = cur
    return prev[-1]


def _fuzzy_hit(sw: str, collapsed_sq: str, max_d: int) -> bool:
    """Опечатка на <=max_d правок: скользящим окном по схлопнутому тексту.
    Только для длинных стоп-слов (>=5), чтобы «спам»/«храм» не путались."""
    if max_d <= 0 or len(sw) < 5:
        return False
    win = len(sw)
    hay = collapsed_sq
    if len(hay) < win - max_d:
        return False
    for i in range(0, len(hay) - win + max_d + 1):
        if _levenshtein(sw, hay[i:i + win + max_d], max_d) <= max_d:
            return True
    return False


def find_stopword(text: str, stopwords, fuzzy: bool = False,
                  max_distance: int = 1) -> str | None:
    """Вернуть первое сработавшее стоп-слово или None.

    fuzzy=True ловит обходы: растянутые буквы (спааам),
    разделители (с-п-а-м) и опечатки на 1 символ (спамм) — только для
    пользовательских стоп-слов, мат-фильтр сюда не входит."""
    if not stopwords:
        return None
    norm = normalize(text)
    collapsed = "".join(_words(norm))          # «с п а м» -> «спам» (снимает разделители)
    collapsed_sq = _squeeze(collapsed) if fuzzy else ""
    for sw in stopwords:
        s = sw.lower()
        if not s:
            continue
        if s in norm or s in collapsed:        # быстрый точный путь (как раньше)
            return sw
        if fuzzy:
            s_sq = _squeeze(s)
            if s_sq and s_sq in collapsed_sq:  # растянутые буквы
                return sw
            if _fuzzy_hit(s_sq, collapsed_sq, max_distance):  # опечатки
                return sw
    return None


def is_bad_name(name: str, stopwords, fuzzy: bool = False,
                max_distance: int = 1) -> str | None:
    """Проверка имени/ника вступающего: мат -> 'мат', стоп-слово -> само слово."""
    if not name:
        return None
    if has_profanity(name):
        return "мат в имени"
    sw = find_stopword(name, stopwords, fuzzy, max_distance)
    return f"стоп-слово «{sw}» в имени" if sw else None
