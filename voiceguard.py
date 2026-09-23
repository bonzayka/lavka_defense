# -*- coding: utf-8 -*-
"""
Модуль расшифровки голосовых сообщений (ГС) и видеозаметок («кружочков»).

Архитектура:
1) Конвертация: Telegram OGG Opus / MP4 -> WAV (16kHz, 16-bit mono) через системный ffmpeg.
2) Распознавание: SpeechRecognition (Google Speech Recognition API, бесплатно, без токена, поддержка ru-RU).
3) Асинхронность: ресурсоёмкие операции вынесены в asyncio.to_thread и ограничены семафором VOICE_CONCURRENCY.
4) Модерация: проверка расшифрованного текста на мат, стоп-слова и деанон/угрозы.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

import config
import deanon
import storage
import textguard

log = logging.getLogger("antispam")

# Семафор для ограничения одновременной обработки аудио (CPU/сеть)
_sema: asyncio.Semaphore | None = None
_whisper_instance: "WhisperModel | None" = None


def _get_semaphore() -> asyncio.Semaphore:
    global _sema
    if _sema is None:
        concurrency = getattr(config, "VOICE_CONCURRENCY", 3)
        _sema = asyncio.Semaphore(concurrency)
    return _sema


def has_ffmpeg() -> bool:
    """Проверка наличия ffmpeg в системе."""
    return bool(shutil.which("ffmpeg"))


def has_whisper() -> bool:
    """Установлена ли библиотека faster-whisper."""
    return WhisperModel is not None


def _get_whisper_model():
    """Ленивая загрузка Whisper (0 МБ RAM на старте, загружается только при необходимости)."""
    global _whisper_instance
    if WhisperModel is None:
        return None
    if _whisper_instance is None:
        model_name = getattr(config, "WHISPER_MODEL", "tiny")
        log.info("voiceguard: ленивая загрузка локальной модели Whisper '%s'...", model_name)
        try:
            _whisper_instance = WhisperModel(model_name, device="cpu", compute_type="int8")
        except Exception:
            _whisper_instance = WhisperModel(model_name, device="cpu", compute_type="float32")
    return _whisper_instance


def available() -> bool:
    """Доступен ли модуль расшифровки (SpeechRecognition или Faster-Whisper + ffmpeg, либо микросервис инференса)."""
    if getattr(config, "INFERENCE_MODE", "microservice") == "microservice":
        return True
    return bool((sr is not None or has_whisper()) and has_ffmpeg())



def convert_to_wav(data: bytes, ext: str = "ogg") -> bytes:
    """Синхронная конвертация входных аудио/видео байт в 16кГц 1-канальный WAV через ffmpeg."""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg не найден в системе")

    suffix_in = f".{ext.lstrip('.')}"
    with tempfile.NamedTemporaryFile(suffix=suffix_in, delete=False) as in_f:
        in_name = in_f.name
        in_f.write(data)

    out_name = in_name + ".wav"
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", in_name,
            "-vn",                  # без видео (для video_note / кружочков)
            "-ar", "16000",         # 16 kHz
            "-ac", "1",             # mono
            "-c:a", "pcm_s16le",    # 16-bit PCM
            "-f", "wav",
            out_name,
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if res.returncode != 0 or not os.path.exists(out_name):
            raise RuntimeError(f"ffmpeg завершился с ошибкой: код {res.returncode}")

        with open(out_name, "rb") as out_f:
            return out_f.read()
    finally:
        for p in (in_name, out_name):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _sync_recognize_whisper_file(wav_path: str, language: str = "ru") -> str | None:
    """Оффлайн-распознавание аудио через локальную модель faster-whisper."""
    model = _get_whisper_model()
    if model is None:
        return None
    try:
        lang_code = language.split("-")[0].lower() if language else "ru"
        segments, _ = model.transcribe(wav_path, language=lang_code, beam_size=1)
        return " ".join(seg.text for seg in segments).strip()
    except Exception as e:
        log.warning("voiceguard: ошибка оффлайн-распознавания Whisper: %s", e)
        return None


def _sync_recognize_file(wav_path: str, language: str) -> str | None:
    """Синхронное распознавание из готового wav-файла."""
    engine = getattr(config, "VOICE_ENGINE", "auto")

    # Если принудительно задан движок whisper
    if engine == "whisper" and has_whisper():
        return _sync_recognize_whisper_file(wav_path, language)

    # 1. Попытка через Google Speech Recognition
    if sr is not None:
        r = sr.Recognizer()
        try:
            with sr.AudioFile(wav_path) as source:
                audio_data = r.record(source)
            try:
                text = r.recognize_google(audio_data, language=language)
                return (text or "").strip()
            except sr.UnknownValueError:
                # Тишина или неразборчивый звук
                return ""
            except sr.RequestError as e:
                log.info("voiceguard: Google API временно недоступен (%s). Переключаемся на резервный Faster-Whisper...", e)
        except Exception as e:
            log.warning("voiceguard: ошибка чтения аудио в SpeechRecognition: %s", e)

    # 2. Резервный оффлайн-движок Faster-Whisper (если Google упал или недоступен)
    if has_whisper():
        return _sync_recognize_whisper_file(wav_path, language)

    return None


def _sync_convert_and_recognize(data: bytes, ext: str, language: str) -> str | None:
    """Конвертация и распознавание за один проход без повторной записи файла на диск."""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg не найден в системе")

    suffix_in = f".{ext.lstrip('.')}"
    with tempfile.NamedTemporaryFile(suffix=suffix_in, delete=False) as in_f:
        in_name = in_f.name
        in_f.write(data)

    out_name = in_name + ".wav"
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", in_name,
            "-vn",                  # без видео (для video_note / кружочков)
            "-ar", "16000",         # 16 kHz
            "-ac", "1",             # mono
            "-c:a", "pcm_s16le",    # 16-bit PCM
            "-f", "wav",
            out_name,
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if res.returncode != 0 or not os.path.exists(out_name):
            raise RuntimeError(f"ffmpeg завершился с ошибкой: код {res.returncode}")

        return _sync_recognize_file(out_name, language)
    finally:
        for p in (in_name, out_name):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _sync_recognize(wav_bytes: bytes, language: str) -> str | None:
    """Синхронное распознавание речи (Google Speech API с фоллбэком на Faster-Whisper)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_f:
        wav_name = wav_f.name
        wav_f.write(wav_bytes)

    try:
        return _sync_recognize_file(wav_name, language)
    finally:
        try:
            if os.path.exists(wav_name):
                os.remove(wav_name)
        except OSError:
            pass


async def transcribe(audio_bytes: bytes, language: str | None = None, ext: str = "ogg") -> str | None:
    """
    Асинхронная расшифровка аудио/видео байт в текст.
    Возвращает:
      - распознанный текст (str);
      - пустую строку "", если речь не распознана / тишина;
      - None в случае сбоя конвертации или недоступности сервиса.
    """
    lang = language or getattr(config, "VOICE_LANGUAGE", "ru-RU")

    # 1. Попытка через изолированный микросервис инференса (IPC)
    if getattr(config, "INFERENCE_MODE", "microservice") == "microservice":
        try:
            import inference_client
            client = inference_client.get_client()
            res = await client.transcribe_audio(audio_bytes, ext=ext, language=lang)
            if res is not None:
                return res
        except Exception as e:
            log.warning("voiceguard: ошибка IPC-микросервиса: %s, пробую локальный фоллбэк...", e)

    # 2. Локальный фоллбэк (если микросервис выключен или недоступен)
    if not bool((sr is not None or has_whisper()) and has_ffmpeg()):
        log.warning("voiceguard: локальный модуль недоступен (проверьте speech_recognition/whisper и ffmpeg)")
        return None

    sema = _get_semaphore()
    async with sema:
        try:
            return await asyncio.to_thread(_sync_convert_and_recognize, audio_bytes, ext, lang)
        except Exception as e:
            log.warning("voiceguard: ошибка обработки аудио: %s", e)
            return None



def scan_for_violations(text: str) -> tuple[bool, str, str, str]:
    """
    Проверяет расшифрованный текст на нарушения (мат, стоп-слова, деанон/угрозы).
    Возвращает (is_violation, public_reason, action, audit_detail).
    """
    if not text:
        return False, "", "none", ""

    # 1. Анти-деанон / угрозы в тексте
    if getattr(config, "TEXT_DEANON_ENABLED", True):
        min_hits = storage.get_num("DEANON_MIN_HITS", getattr(config, "DEANON_MIN_HITS", 2))
        hit, why = deanon.scan_text(text, min_hits)
        if hit:
            act = storage.get_str("TEXT_DEANON_ACTION", getattr(config, "TEXT_DEANON_ACTION", "mute"))
            return True, "деанон/угроза в ГС", act, f"деанон-текст в ГС: {why}"

    # 2. Мат
    antimat = storage.get_flag("ANTIMAT_ENABLED", getattr(config, "ANTIMAT_ENABLED", True))
    if antimat and textguard.has_profanity(text):
        act = storage.get_str("TEXT_ACTION", getattr(config, "TEXT_ACTION", "mute"))
        return True, "мат в ГС", act, "нецензурная лексика в голосовом сообщении"

    # 3. Стоп-слова
    sw_list = storage.stopwords()
    fuzzy = storage.get_flag("FUZZY_STOPWORDS", getattr(config, "FUZZY_STOPWORDS", True))
    max_d = storage.get_num("FUZZY_MAX_DISTANCE", getattr(config, "FUZZY_MAX_DISTANCE", 1))
    sw = textguard.find_stopword(text, sw_list, fuzzy=fuzzy, max_distance=max_d)
    if sw:
        act = storage.get_str("TEXT_ACTION", getattr(config, "TEXT_ACTION", "mute"))
        if storage.is_hidden_word(sw):
            return True, "нарушение правил в ГС", act, f"скрытое стоп-слово «{sw}» в ГС"
        return True, f"стоп-слово «{sw}» в ГС", act, f"стоп-слово «{sw}» в ГС"

    return False, "", "none", ""
