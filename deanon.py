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
HEAVY_TYPES = {"passport_ru", "card", "snils"}

# Российский/международный телефон: +7 999 123-45-67, 8(999)1234567, +1..., +81...
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d[\s\-()]?){10,15}(?!\d)")
# Но телефоном считаем только если после чистки 10–15 цифр и есть код/формат.
_PHONE_DIGITS = re.compile(r"\d")

# Email.
_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)

# Ссылка на профиль / @username (деанон часто «вот его тг: @...»).
_HANDLE = re.compile(r"(?:https?://|t\.me/|@)[a-z0-9_]{4,}", re.I)

# Номер банковской карты: 16 цифр группами по 4 (иногда 4-6-5 для Маэстро).
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")

# Паспорт РФ: серия 4 цифры + номер 6 цифр (10 цифр, часто «12 34 567890»).
_PASSPORT_RU = re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?\d{6}(?!\d)")

# СНИЛС: 11 цифр «123-456-789 01». Требуем РАЗДЕЛИТЕЛИ (иначе ловит 11-значные
# телефоны 89991234567). Хотя бы один дефис/пробел между группами.
_SNILS = re.compile(r"(?<!\d)\d{3}[\-\s]\d{3}[\-\s]\d{3}[\-\s]\d{2}(?!\d)")

# ИНН физлица: 12 цифр подряд.
_INN = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Адрес: маркеры «ул./улица/пр-т/д. 12 кв. 5/г. Москва/индекс».
_ADDRESS = re.compile(
    r"(?:\bул(?:\.|ица)\b|\bпроспект\b|\bпр[\-\s]?т\b|\bпереул|\bбульвар\b|"
    r"\bшоссе\b|\bмкр\b|\bкв(?:\.|артира)\b|\bкорп(?:\.|ус)\b|\bдом\s+\d|"
    r"\bд\.\s?\d+.{0,12}\bкв\.?\s?\d+|\bиндекс\b|\bг\.\s?[А-ЯЁ][а-яё]{2,}|"
    # dotless-варианты (частый деанон: «г Москва ул Ленина д5 кв10»)
    r"\bг\s+[А-ЯЁ][а-яё]{2,}|\bул\s+[А-ЯЁ][а-яё]{2,}|"
    r"\bкв\s?\d{1,4}\b|\bд\s?\d{1,4}\s*кв)",
    re.I)

# Явные подписи «паспорт/карта/адрес проживания» усиливают уверенность.
_LABELS = re.compile(
    r"(паспорт|снилс|инн\b|карта\s+\d|номер\s+карты|адрес\s+прожив|"
    r"прописк|домашн\w*\s+адрес|дата\s+рожд|\bдр\b\s*[:\-])",
    re.I)


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


def _looks_like_phone(raw: str) -> bool:
    d = _clean_digits(raw)
    if not (10 <= len(d) <= 15):
        return False
    # РФ (+7/8 + 10 цифр) либо международный с ведущим кодом.
    return d.startswith(("7", "8", "9")) or raw.strip().startswith("+")


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
    found: set[str] = set()

    if _EMAIL.search(raw):
        found.add("email")
    if _HANDLE.search(raw):
        found.add("handle")

    for m in _CARD.finditer(raw):
        if _luhn_ok(_clean_digits(m.group())):
            found.add("card")
            break

    if _PASSPORT_RU.search(raw):
        found.add("passport_ru")
    if _SNILS.search(raw):
        found.add("snils")
    # ИНН — только если 12 цифр не «съедены» картой/паспортом выше; отдельный тип.
    if _INN.search(raw):
        found.add("inn")

    for m in _PHONE.finditer(raw):
        if _looks_like_phone(m.group()):
            found.add("phone")
            break

    if _ADDRESS.search(raw) or _ADDRESS.search(norm):
        found.add("address")
    if _LABELS.search(raw) or _LABELS.search(norm):
        found.add("label")

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
    r"пробь?ю по", r"проб[еи]в\b", r"деанон", r"сдеаноню", r"задеаноню",
    r"сол[ьъе]ю (?:твои|его|е[её]|ваши)?\s*дан", r"слив дан", r"сливаю дан",
    r"выложу (?:твой|его|её|ваш)\s*(?:адрес|номер|паспорт|данные)",
    r"скину (?:твой|его|её)\s*(?:адрес|номер|паспорт)",
    r"твой адрес\b", r"по ip\b", r"пробить по номеру",
]
_THREAT_RE = [re.compile(p, re.I) for p in _THREAT_PATTERNS]

# Ники/ссылки деанон-ресурсов: @chudochatdnn, t.me/deanonbaza, пробив-боты.
_DEANON_HANDLE_RE = re.compile(
    r"(?:@|t\.me/|https?://[^\s@]*?/)\w*?(?:deanon|деанон|dnn|probiv|пробив|"
    r"слив|dox|докс|leakb|leaked)\w*",
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
}


def describe(types: list[str]) -> str:
    return ", ".join(TYPE_LABELS.get(t, t) for t in types)
