# -*- coding: utf-8 -*-
"""
Детектор 18+ (порно/нагота) — ViT-классификатор целым кадром.

В отличие от детекторов частей тела (NudeNet/ifnude), которые ищут отдельные
куски тела и потому мажут на нестандартных ракурсах и дают ложные срабатывания,
эта модель смотрит на картинку ЦЕЛИКОМ и выдаёт одну вероятность «nsfw».
Быстрее (~0.4 с на 4 vCPU) и устойчивее к ракурсам.

Модель: AdamCodd/vit-base-nsfw-detector (ViT-base-384), метки {0: sfw, 1: nsfw}.
Работает на onnxruntime — torch/transformers НЕ нужны. Веса (ONNX) скачиваются
один раз автоматически в папку models/ рядом с ботом (или по env NSFW_MODEL_DIR).
"""

import io
import logging
import os
import urllib.request

import numpy as np
from PIL import Image

log = logging.getLogger("antispam")

_session = None
_input_name = None
_loaded = False
load_error = ""   # текст ошибки последней попытки загрузки (для /diag)

# Препроцессинг из preprocessor_config.json модели: resize 384, /255, норм [0.5].
_SIZE = 384
_MEAN = np.array([0.5, 0.5, 0.5], dtype=np.float32)
_STD = np.array([0.5, 0.5, 0.5], dtype=np.float32)

MAX_BYTES = 12 * 1024 * 1024  # очень большие картинки не гоняем

_HF = "https://huggingface.co/AdamCodd/vit-base-nsfw-detector/resolve/main/onnx"
_FILES = {                    # вариант -> (имя файла, url, примерный размер МБ)
    "quantized": ("model_quantized.onnx", f"{_HF}/model_quantized.onnx", 84),
    "full":      ("model.onnx",           f"{_HF}/model.onnx",           329),
}


def _model_dir() -> str:
    d = os.environ.get("NSFW_MODEL_DIR")
    if not d:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_model(variant: str) -> str:
    """Вернуть путь к .onnx, скачав его при отсутствии. Бросает при неудаче."""
    fname, url, mb = _FILES.get(variant, _FILES["quantized"])
    path = os.path.join(_model_dir(), fname)
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        return path
    tmp = path + ".part"
    log.info("NSFW: качаю модель %s (~%d МБ) в %s …", fname, mb, path)
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, path)
    log.info("NSFW: модель скачана (%d МБ).", os.path.getsize(path) // 1024 // 1024)
    return path


def load(variant: str = "quantized", threads: int = 0) -> None:
    """Поднять ViT-классификатор (один раз). Тихо отключается при ошибке."""
    global _session, _input_name, _loaded, load_error
    try:
        import onnxruntime as ort
        path = _ensure_model(variant)
        so = ort.SessionOptions()
        if threads and threads > 0:
            so.intra_op_num_threads = threads
        _session = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        _input_name = _session.get_inputs()[0].name
        _loaded = True
        load_error = ""
        # прогрев (первый прогон компилирует граф)
        try:
            _run(Image.new("RGB", (_SIZE, _SIZE)))
        except Exception:
            pass
        log.info("NSFW-детектор (ViT %s) загружен.", variant)
    except Exception as e:
        load_error = f"{type(e).__name__}: {e}"
        _loaded = False
        log.warning("NSFW-детектор (ViT) НЕ загрузился: %s", load_error)


def status() -> str:
    if _loaded:
        return "✅ загружен"
    return f"❌ не загружен ({load_error})" if load_error else "❌ выключен"


def available() -> bool:
    return _loaded


def _preprocess(img: Image.Image) -> np.ndarray:
    img = img.convert("RGB").resize((_SIZE, _SIZE), Image.BILINEAR)
    a = np.asarray(img, dtype=np.float32) / 255.0
    a = (a - _MEAN) / _STD
    return a.transpose(2, 0, 1)[None, :].astype(np.float32)  # NCHW


def _run(img: Image.Image) -> float:
    logits = _session.run(None, {_input_name: _preprocess(img)})[0][0]
    e = np.exp(logits - logits.max())
    return float((e / e.sum())[1])  # индекс 1 = nsfw


def detect_prob(data: bytes) -> float | None:
    """Вероятность 18+ (0..1) для картинки в байтах. None — если недоступно/ошибка.
    Синхронно; звать через asyncio.to_thread."""
    if not _loaded or not data or len(data) > MAX_BYTES:
        return None
    try:
        return _run(Image.open(io.BytesIO(data)))
    except Exception as e:
        log.debug("NSFW ViT detect fail: %s", e)
        return None
