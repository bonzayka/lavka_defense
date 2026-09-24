# -*- coding: utf-8 -*-
"""
Анти-деанон: распознаёт на КАРТИНКАХ чужие персональные данные (скриншоты с
телефонами, адресами, паспортами, номерами карт, профилями) — типичный кейс
«деанонщик кидает скрин с данными жертвы в чат».

Два слоя, оба разделены ради тестируемости:

  1) find_pii(text) — ЧИСТАЯ функция (regex, без сети/OCR): по тексту находит
     персональные данные и возвращает список типов. Её гоняют юниты в tests.py.

  2) OCR-обёртка (extract_text) — достаёт текст с картинки. Движок грузится
     ЛЕНИВО и УСТОЙЧИВО: если OCR не установлен, детектор просто отключается
     (available()=False) и бот работает как раньше — картинки идут дальше по
     обычному пути (dhash/NSFW/гор).

Приоритет OCR-движка:
  • rapidocr-onnxruntime — на onnxruntime (уже в стеке бота), НЕ требует
    системного бинарника, из коробки читает rus+eng. Рекомендуется.
      pip install rapidocr-onnxruntime
  • pytesseract — если установлен пакет И системный `tesseract` (+ языки
    rus/eng). Фолбэк.
      pip install pytesseract   (+ пакет tesseract-ocr, tesseract-ocr-rus)

Важно: OCR — ЭВРИСТИКА. Чтобы не мутить за случайный скрин переписки, в боте
срабатывание требует НЕСКОЛЬКО разных типов данных (config.DEANON_MIN_HITS)
либо один «тяжёлый» маркер (паспорт/карта/СНИЛС).
"""

import io
import logging
import re

log = logging.getLogger("antispam")

try:                       # нормализация гомоглифов/невидимок — как в остальном боте
    import textguard
except Exception:          # pragma: no cover — на всякий случай, без него тоже ок
    textguard = None

# ---------------------------------------------------------------------------
# Слой 1: чистый детектор PII (персональных данных) по тексту.
# ---------------------------------------------------------------------------

# «Тяжёлые» типы — одного достаточно для срабатывания (почти не бывают случайно).
# ИНН НЕ тяжёлый: голые 12 цифр = межд. номер/код, легко ложит.
HEAVY_TYPES = {"passport_ru", "card", "snils", "osint_card"}

# Регулярка для маскирования ссылок при поиске числовых ПДн (паспорта, телефоны, карты, СНИЛС, ИНН).
# Исключает ложные срабатывания по ref-кодам, ID юзеров в ссылках бота (t.me/...bot?start=ref7475771830),
# номерам статей, ID заказов в URL и т.д.
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|tg://\S+", re.I)


def _mask_urls(text: str) -> str:
    """Заменить ссылки в тексте на пробелы той же длины для поиска чисел (паспорта/телефоны/карты)."""
    return _URL_RE.sub(lambda m: " " * len(m.group()), text)


# Российский/международный телефон:
# 1) Форматированный номер РФ (+7/8 с разделителями, скобками или дефисами)
_PHONE_RU_FORMATTED = re.compile(
    r"(?<![a-zA-Z0-9_])(?:\+7|8)[\s\-]?(?:\(\s*\d{3}\s*\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?![a-zA-Z0-9_])"
)
# 2) Компактный мобильный РФ: +79XXXXXXXXX, 79XXXXXXXXX или 89XXXXXXXXX (11 цифр с кодом 9xx)
_PHONE_RU_COMPACT = re.compile(
    r"(?<![a-zA-Z0-9_])(?:\+?7|8)9\d{9}(?![a-zA-Z0-9_])"
)
# 3) 10-значный номер мобильного РФ только при явном формате: (9xx) xxx-xx-xx или 9xx-xxx-xx-xx
_PHONE_RU_10D_FORMATTED = re.compile(
    r"(?<![a-zA-Z0-9_])(?:\(\s*9\d{2}\s*\)|9\d{2})[\s\-](\d{3})[\s\-](\d{2})[\s\-](\d{2})(?![a-zA-Z0-9_])"
)
# 4) Международный телефон с префиксом + (от 10 до 15 цифр)
_PHONE_INTL = re.compile(
    r"(?<![a-zA-Z0-9_])\+\d{1,4}[\s\-()]?\d{1,4}[\s\-()]?\d{2,4}[\s\-()]?\d{2,4}(?![a-zA-Z0-9_])"
)

# Email.
_EMAIL = re.compile(r"(?<![a-zA-Z0-9_.])[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}(?![a-zA-Z0-9_])", re.I)

# Ссылка на личный профиль / @username в соцсетях:
# @ник (не бот, не email-домен)
_HANDLE_TG = re.compile(r"(?<![a-zA-Z0-9_.])@([a-zA-Z][a-zA-Z0-9_]{3,31})(?![a-zA-Z0-9_])", re.I)
# t.me/ник (не бот, не служебные ссылки t.me/joinchat, t.me/c/...)
_HANDLE_TME = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?!joinchat\b|c/\b|\+|share\b|addstickers\b|invoice\b)([a-zA-Z][a-zA-Z0-9_]{3,31})(?:[/?#\s]|$)",
    re.I
)
# Профили соцсетей (vk, ok, insta, fb)
_HANDLE_SOCIAL = re.compile(
    r"(?:https?://)?(?:vk\.com|vkontakte\.ru|ok\.ru|instagram\.com|facebook\.com)/([a-zA-Z0-9_.]{3,32})",
    re.I
)

# Номер банковской карты: 13-19 цифр с валидацией по алгоритму Луна.
_CARD = re.compile(r"(?<![a-zA-Z0-9_])(?:\d[ \-]?){13,19}(?![a-zA-Z0-9_])")

# Паспорт РФ:
# 1) Форматированный: серия 4 цифры (код региона 01-99) + разделитель + номер 6 цифр
#    Примеры: «12 34 567890», «1234 567890», «6501 429135», «серия 6501 номер 429135»
_PASSPORT_FORMATTED = re.compile(
    r"(?<![a-zA-Z0-9_])(0[1-9]|[1-9]\d)\s?(\d{2})(?:[\s\-–—№#]|номер|№)+\s*(\d{6})(?![a-zA-Z0-9_])",
    re.I
)
# 2) 10 цифр подряд ТОЛЬКО при наличии маркера паспорта рядом (иначе 10 цифр = ID/таймстамп/код)
_PASSPORT_LABELED = re.compile(
    r"(?:\bпаспорт\w*|\bпасп\.?|\bсерия\s*(?:и|№)?\s*номер|\bдокумент\w*)\s*[:№\-]?\s*(?<![a-zA-Z0-9_])(0[1-9]|[1-9]\d)(\d{8})(?![a-zA-Z0-9_])|"
    r"(?<![a-zA-Z0-9_])(0[1-9]|[1-9]\d)(\d{8})(?![a-zA-Z0-9_])\s*[:№\-]?\s*(?:\bпаспорт\w*|\bпасп\.?)",
    re.I
)

# СНИЛС: 11 цифр «123-456-789 01». Требуем разделители либо подпись.
_SNILS = re.compile(r"(?<![a-zA-Z0-9_])\d{3}[\-\s]\d{3}[\-\s]\d{3}[\-\s]\d{2}(?![a-zA-Z0-9_])")
_SNILS_LABELED = re.compile(r"(?:\bснилс\b|\bsnils\b)\s*[:№\-]?\s*(\d{11}|\d{3}[\-\s]?\d{3}[\-\s]?\d{3}[\-\s]?\d{2})", re.I)

# ИНН: 10 или 12 цифр с подписью, либо с валидной контрольной суммой
_INN_LABELED = re.compile(r"(?:\bинн\b|\binn\b)\s*[:№\-]?\s*(\d{10}|\d{12})\b", re.I)
_INN_BARE = re.compile(r"(?<![a-zA-Z0-9_])(\d{10}|\d{12})(?![a-zA-Z0-9_])")

# Адрес: маркеры «ул./улица/пр-т/д. 12 кв. 5/г. Москва/индекс».
_ADDRESS = re.compile(
    r"(?:\bул(?:\.|ица)\b|\bпроспект\b|\bпр[\-\s]?т\b|\bпереул|\bбульвар\b|"
    r"\bшоссе\b|\bмкр\b|\bкв(?:\.|артира)\b|\bкорп(?:\.|ус)\b|\bдом\s+\d|"
    r"\bд\.\s?\d+.{0,12}\bкв\.?\s?\d+|\bиндекс\b|\bг\.\s?[А-ЯЁ][а-яё]{2,}|"
    # dotless-варианты (частый деанон: «г Москва ул Ленина д5 кв10»)
    r"\bг\s+[А-ЯЁ][а-яё]{2,}|\bул\s+[А-ЯЁ][а-яё]{2,}|"
    r"\bкв\s?\d{1,4}\b|\bд\s?\d{1,4}\s*кв)",
    re.I)

# Явные подписи «паспорт/карта/адрес проживания/ФИО» усиливают уверенность.
_LABELS = re.compile(
    r"(паспорт|снилс|инн\b|карта\s+\d|номер\s+карты|адрес\s+прожив|"
    r"адрес\s+регистр|прописк|домашн\w*\s+адрес|дата\s+рожд|\bдр\b\s*[:\-]|"
    r"кем\s+выдан|код\s+подразделения|(?:\bф\s*\.\s*и\s*\.\s*о\s*\.?(?!\w)|\bфио\b)|фамили[яи]\b|имя\b|отчество\b|основные\s+данные)",
    re.I)

# Окончания отчеств в РФ (мужские и женские):
_PATRONYMIC_ENDINGS = r"(?:ович|евич|ич|ыч|овна|евна|ична|инична|ычна)"

# 3-составное ФИО: [Фамилия] [Имя] [Отчество] или [Имя] [Отчество] [Фамилия]
_FIO_3PART = re.compile(
    rf"\b([А-ЯЁ][а-яё]{{1,25}})\s+([А-ЯЁ][а-яё]{{1,25}})\s+([А-ЯЁ][а-яё]{{1,25}}{_PATRONYMIC_ENDINGS})\b|"
    rf"\b([А-ЯЁ][а-яё]{{1,25}})\s+([А-ЯЁ][а-яё]{{1,25}}{_PATRONYMIC_ENDINGS})\s+([А-ЯЁ][а-яё]{{1,25}})\b"
)

# ФИО с явной меткой (ФИО:, Ф.И.О., полное имя, ├ ФИО и т.п.):
_FIO_LABELED = re.compile(
    r"(?:(?:\bф\s*\.\s*и\s*\.\s*о\s*\.?(?!\w))|\bфио\b|полное\s+имя|[├└│\-•*#]\s*(?:фио|ф\.?\s*и\.?\s*о\.?))\s*[:—\-]?\s*"
    r"([А-ЯЁа-яё][а-яё]+[\s\-]+[А-ЯЁа-яё][а-яё]+(?:[\s\-]+[А-ЯЁа-яё]+)?)",
    re.I
)

# ФИО разбитое по строкам/полям (Фамилия: ... Имя: ...):
_FIO_SPLIT = re.compile(
    r"(?:фамили[яи]\b\s*[:—\-]?\s*([А-ЯЁа-яё]+)).{0,40}?(?:имя\b\s*[:—\-]?\s*([А-ЯЁа-яё]+))|"
    r"(?:имя\b\s*[:—\-]?\s*([А-ЯЁа-яё]+)).{0,40}?(?:фамили[яи]\b\s*[:—\-]?\s*([А-ЯЁа-яё]+))",
    re.I | re.S
)

# Известные исторические/литературные личности для исключения ложняков на цитатах:
_FAMOUS_PERSONS = {
    "пушкин", "толстой", "достоевский", "чехов", "ленин", "гагарин",
    "лермонтов", "гоголь", "чайковский", "есенин", "маяковский",
    "тургенев", "булгаков", "некрасов", "ломоносов"
}


def _find_3part_fio(text: str) -> list[str]:
    matches = []
    for m in _FIO_3PART.finditer(text):
        words = [w for w in m.groups() if w]
        if any(w.lower() in _FAMOUS_PERSONS for w in words):
            continue
        matches.append(" ".join(words))
    return matches


# Дата рождения:
_BIRTHDATE = re.compile(
    r"(?:\bдата\s+рожд\w*|\bд\.?р\.?\b|[├└│\-•]\s*дата\s+рожд\w*)\s*[:—\-]?\s*"
    r"(\d{2}[./\-]\d{2}[./\-]\d{4})|"
    r"(?:[├└│\-•]\s*возраст\b|\bвозраст\b)\s*[:—\-]?\s*(\d{1,3}\b)",
    re.I
)
_DATE_DMY = re.compile(r"\b(0[1-9]|[12]\d|3[01])[./\-](0[1-9]|1[0-2])[./\-](19\d{2}|20[0-2]\d)\b")

# Карточка Глаз Бога / OSINT-пробива (шаблон карточки досье):
_OSINT_CARD = re.compile(
    r"(?:👤\s*(?:основные\s+данные|результат\s+поиска|досье|информация|пробив)|"
    r"[├└│]\s*фио\b|"
    r"фио\b.{1,50}\b(?:дата\s+рожд\w*|возраст\b|др\b)|"
    r"(?:дата\s+рожд\w*|возраст\b|др\b).{1,50}\bфио\b)",
    re.I
)


def _is_standalone_phone(raw: str) -> bool:
    """Проверяет, состоит ли всё сообщение практически целиком из одного номера телефона (слив номера)."""
    stripped = raw.strip()
    if len(stripped) > 30:
        return False
    d = _clean_digits(stripped)
    if len(d) == 11 and (d.startswith(("7", "8")) or stripped.startswith("+")):
        non_phone = re.sub(r"[\d\s+\-()илтномерTELphone:]", "", stripped, flags=re.I)
        return len(non_phone) == 0
    return False


def _is_standalone_fio(raw: str) -> bool:
    """Проверяет, является ли короткое сообщение одиночным вбросом ФИО (сливом имени)."""
    stripped = raw.strip()
    if len(stripped) > 90:
        return False
    fios = _find_3part_fio(stripped)
    if fios:
        rem = stripped
        for f in fios:
            rem = rem.replace(f, "")
        rem_clean = re.sub(
            r"[\s\.,:;!?\"'«»—\-\(\)👤├└│•*#]|это\b|вот\b|он\b|она\b|фио\b|ф\.?\s*и\.?\s*о\.?",
            "", rem, flags=re.I
        )
        if len(rem_clean) <= 12:
            return True
    if _FIO_LABELED.search(stripped) or _FIO_SPLIT.search(stripped):
        rem_clean = re.sub(
            r"[\s\.,:;!?\"'«»—\-\(\)👤├└│•*#]|это\b|вот\b|он\b|она\b|фио\b|ф\.?\s*и\.?\s*о\.?|фамили[яи]\b|имя\b|отчество\b",
            "", stripped, flags=re.I
        )
        if len(rem_clean) <= 40:
            return True
    return False


def _clean_digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _luhn_ok(num: str) -> bool:
    """Проверка номера карты по алгоритму Луна (отсекает случайные 16 цифр)."""
    if not (13 <= len(num) <= 19):
        return False
    total, alt = 0, False
    for ch in reversed(num):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _check_inn_checksum(num: str) -> bool:
    """Контрольная сумма ИНН (10 цифр ЮЛ или 12 цифр ФЛ/ИП)."""
    if len(num) == 10:
        c = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(int(num[i]) * c[i] for i in range(9)) % 11 % 10 == int(num[9])
    if len(num) == 12:
        c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        s11 = sum(int(num[i]) * c11[i] for i in range(10)) % 11 % 10 == int(num[10])
        s12 = sum(int(num[i]) * c12[i] for i in range(11)) % 11 % 10 == int(num[11])
        return bool(s11 and s12)
    return False


def _looks_like_phone(raw: str) -> bool:
    d = _clean_digits(raw)
    if not (10 <= len(d) <= 15):
        return False
    # РФ (11 цифр с кодом +7/8 либо 10 цифр без кода страны, начинающиеся с 9)
    if len(d) == 10:
        return d.startswith("9")
    if len(d) == 11:
        return d.startswith(("7", "8"))
    return raw.strip().startswith("+")


def find_pii(text: str) -> list[str]:
    """Вернуть отсортированный список типов найденных персональных данных.

    Типы: 'phone', 'email', 'handle', 'card', 'passport_ru', 'snils', 'inn',
    'address', 'label'. Пустой список — ничего не нашли.
    """
    if not text:
        return []
    norm = textguard.normalize(text) if textguard else text.lower()
    # Для regex по цифрам/латинице берём исходный текст (нормализация ломает цифры
    # гомоглифами вроде 0->о), а для «словесных» маркеров — нормализованный.
    raw = text
    # Для поиска чисел маскируем URL, чтобы параметры ссылок (?start=ref7475771830)
    # не принимались за паспорта или телефоны.
    num_text = _mask_urls(raw)
    found: set[str] = set()

    # Email
    emails = {m.group().lower() for m in _EMAIL.finditer(raw)}
    if emails:
        found.add("email")

    # Личные профили / @handle (не боты и не домены почты)
    for m in _HANDLE_TG.finditer(raw):
        u = m.group(1).lower()
        if not u.endswith("bot") and not any(u in em for em in emails):
            found.add("handle")
            break
    if "handle" not in found:
        for m in _HANDLE_TME.finditer(raw):
            u = m.group(1).lower()
            if not u.endswith("bot"):
                found.add("handle")
                break
    if "handle" not in found and _HANDLE_SOCIAL.search(raw):
        found.add("handle")

    # Банковские карты
    for m in _CARD.finditer(num_text):
        if _luhn_ok(_clean_digits(m.group())):
            found.add("card")
            break

    # Паспорт РФ
    if _PASSPORT_FORMATTED.search(num_text) or _PASSPORT_LABELED.search(num_text):
        found.add("passport_ru")

    # СНИЛС
    if _SNILS.search(num_text) or _SNILS_LABELED.search(num_text):
        found.add("snils")

    # ИНН
    if _INN_LABELED.search(num_text):
        found.add("inn")
    else:
        for m in _INN_BARE.finditer(num_text):
            if _check_inn_checksum(m.group()):
                found.add("inn")
                break

    # Телефон (в тексте с замаскированными ссылками)
    if (_PHONE_RU_FORMATTED.search(num_text) or
        _PHONE_RU_COMPACT.search(num_text) or
        _PHONE_RU_10D_FORMATTED.search(num_text) or
        _PHONE_INTL.search(num_text)):
        found.add("phone")

    # Адрес
    if _ADDRESS.search(raw) or _ADDRESS.search(norm):
        found.add("address")

    # Подписи
    if _LABELS.search(raw) or _LABELS.search(norm):
        found.add("label")

    # ФИО (по явным меткам, сплит-полям или 3-составному имени с отчеством)
    if _FIO_LABELED.search(raw) or _FIO_SPLIT.search(raw) or _find_3part_fio(raw):
        found.add("fio")

    # Дата рождения / возраст
    if _BIRTHDATE.search(raw) or (_DATE_DMY.search(raw) and ("fio" in found or re.search(r"\bг\.?р\.?\b", raw, re.I))):
        found.add("birthdate")

    # Шаблон карточки OSINT / пробива
    if _OSINT_CARD.search(raw):
        found.add("osint_card")

    return sorted(found)


def is_deanon(text: str, min_hits: int = 2) -> tuple[bool, list[str]]:
    """Похоже ли, что на тексте чужие персональные данные.

    Срабатывает, если: есть «тяжёлый» тип (паспорт/карта/СНИЛС/ИНН), ЛИБО набрано
    >= min_hits разных типов. 'label' сам по себе (без данных) не считается
    достаточным, но усиливает: подпись + любой один тип = срабатывание.
    Возвращает (сработало, список_типов).
    """
    types = find_pii(text)
    if not types:
        return False, types
    if HEAVY_TYPES.intersection(types):
        return True, types
    has_label = "label" in types
    data_types = [t for t in types if t != "label"]
    need = max(1, min_hits - 1) if has_label else min_hits
    return (len(data_types) >= need and len(data_types) >= 1), types


# ---------------------------------------------------------------------------
# Слой 1б: угрозы и деанон-ресурсы в ТЕКСТЕ (травля/деанон админов чата).
# ---------------------------------------------------------------------------

# Угрозы/запугивание/деанон-намерение (по нормализованному тексту, кириллица).
_THREAT_PATTERNS = [
    r"уб[ьъе]?ю\b", r"прир[еэ]жу", r"зар[еэ]жу", r"закопа", r"пришью тебя",
    r"взорву", r"сожгу тебя", r"найду тебя", r"я тебя найду", r"приеду к тебе",
    r"знаю где (?:ты )?жив[её]шь", r"вычисл\w* тебя", r"вычислю по",
    r"пробь?ю по", r"\bпроб[ьъ][юе]\b", r"\bпробив\s+(?:по|данных|через|админа|тебя)\b",
    r"\b(?:с|за)?деанон(?:ю|ят|ить|нули|им)?\b",
    r"сол[ьъе]ю (?:твои|его|е[её]|ваши)?\s*дан", r"слив дан", r"сливаю дан",
    r"выложу (?:твой|его|её|ваш)\s*(?:адрес|номер|паспорт|данные)",
    r"скину (?:твой|его|её)\s*(?:адрес|номер|паспорт)",
    r"твой адрес\b", r"по ip\b", r"пробить по номеру",
]
_THREAT_RE = [re.compile(p, re.I) for p in _THREAT_PATTERNS]

# Ники/ссылки деанон-ресурсов: @chudochatdnn, t.me/deanonbaza, пробив-боты.
_DEANON_HANDLE_RE = re.compile(
    r"(?:@|t\.me/|https?://[^\s@]*?/)\w*?(?:deanon|деанон|dnn|probiv|пробив|"
    r"dox|докс|leakb|leaked)\w*",
    re.I)


def find_threats(text: str) -> list[str]:
    """Список сработавших маркеров угроз/запугивания в тексте ([] — чисто)."""
    if not text:
        return []
    norm = textguard.normalize(text) if textguard else text.lower()
    return [rx.pattern for rx in _THREAT_RE if rx.search(norm)]


def find_deanon_handles(text: str) -> list[str]:
    """Ники/ссылки деанон-ресурсов в тексте (@...dnn, t.me/deanon...)."""
    if not text:
        return []
    raw = (text or "").lower()   # латиница как есть; normalize её сломал бы
    return list(dict.fromkeys(m.group() for m in _DEANON_HANDLE_RE.finditer(raw)))


def scan_text(text: str, min_hits: int = 2) -> tuple[bool, str]:
    """Текстовый анти-деанон: угрозы + деанон-ресурсы + чужие ПДн в тексте.

    Кейс — травля/деанон админов чата. Возвращает (сработало, причина-строка).
    """
    reasons = []
    if find_threats(text):
        reasons.append("угроза/запугивание")
    h = find_deanon_handles(text)
    if h:
        reasons.append("деанон-ресурс (%s)" % ", ".join(h[:3]))
    pii_hit, pii_types = is_deanon(text, min_hits)
    if not pii_hit and _is_standalone_phone(text):
        pii_hit = True
        pii_types = ["phone"]
    if not pii_hit and _is_standalone_fio(text):
        pii_hit = True
        pii_types = ["fio"]
    if pii_hit:
        reasons.append("чужие ПДн: " + describe(pii_types))
    return (bool(reasons), "; ".join(reasons))


# ---------------------------------------------------------------------------
# Слой 2: OCR-обёртка (ленивая, устойчивая к отсутствию движка).
# ---------------------------------------------------------------------------

_engine = None            # 'rapidocr' | 'tesseract'
_reader = None            # инстанс движка (для rapidocr)
_loaded = False
load_error = ""

MAX_BYTES = 12 * 1024 * 1024


def load(lang: str = "rus+eng") -> None:
    """Поднять OCR один раз. Тихо отключается, если движок не установлен."""
    global _engine, _reader, _loaded, load_error
    if _loaded:
        return
    # 1) rapidocr-onnxruntime (предпочтительно — onnxruntime уже в стеке).
    try:
        from rapidocr_onnxruntime import RapidOCR
        _reader = RapidOCR()
        _engine = "rapidocr"
        _loaded = True
        load_error = ""
        log.info("Деанон-OCR: rapidocr-onnxruntime загружен.")
        return
    except Exception as e:
        load_error = f"rapidocr: {type(e).__name__}: {e}"

    # 2) pytesseract (нужен системный бинарник tesseract + языки).
    try:
        import pytesseract
        from PIL import Image  # noqa: F401 — проверяем, что доступно
        pytesseract.get_tesseract_version()   # бросит, если бинарника нет
        _engine = "tesseract"
        _loaded = True
        load_error = ""
        log.info("Деанон-OCR: pytesseract загружен.")
        return
    except Exception as e:
        load_error += f" | tesseract: {type(e).__name__}: {e}"

    _loaded = False
    log.warning("Деанон-OCR не загрузился (нет движка): %s", load_error)


def available() -> bool:
    return _loaded


def status() -> str:
    if _loaded:
        return f"✅ загружен ({_engine})"
    return f"❌ не загружен ({load_error})" if load_error else "❌ выключен"


def _ocr_rapid(data: bytes) -> str:
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    result, _ = _reader(np.asarray(img))
    if not result:
        return ""
    return "\n".join(line[1] for line in result if len(line) >= 2)


def _ocr_tesseract(data: bytes, lang: str) -> str:
    import pytesseract
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    try:
        return pytesseract.image_to_string(img, lang=lang)
    except Exception:                       # язык не установлен — пробуем eng
        return pytesseract.image_to_string(img)


def extract_text(data: bytes, lang: str = "rus+eng") -> str:
    """Достать текст с картинки (в байтах). '' — если OCR недоступен/пусто/ошибка.
    Синхронно; звать через asyncio.to_thread."""
    if not _loaded or not data or len(data) > MAX_BYTES:
        return ""
    try:
        if _engine == "rapidocr":
            return _ocr_rapid(data)
        if _engine == "tesseract":
            return _ocr_tesseract(data, lang)
    except Exception as e:
        log.debug("Деанон-OCR extract fail: %s", e)
    return ""


# Человекочитаемые названия типов (для отчёта админу).
TYPE_LABELS = {
    "phone": "телефон",
    "email": "email",
    "handle": "профиль/@ник",
    "card": "номер карты",
    "passport_ru": "паспорт РФ",
    "snils": "СНИЛС",
    "inn": "ИНН",
    "address": "адрес",
    "label": "подпись «паспорт/адрес»",
    "fio": "ФИО",
    "birthdate": "дата рождения",
    "osint_card": "карточка досье/пробива",
}


def describe(types: list[str]) -> str:
    return ", ".join(TYPE_LABELS.get(t, t) for t in types)
