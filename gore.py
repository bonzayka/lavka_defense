# -*- coding: utf-8 -*-
"""
Детектор шок-контента / гора (кровь, трупы, расчленёнка, насилие).

Локально, бесплатно, оффлайн — через CLIP zero-shot: сравниваем картинку с
текстовыми описаниями «плохого» и «нормального» и берём вероятность «плохой»
группы. Модель грузится лениво; если torch/transformers не установлены —
детектор просто отключается (бот работает дальше).

Зависимости (ставить отдельно, тяжёлые):  pip install -r requirements-gore.txt
На Linux ставь CPU-версию torch, иначе подтянется огромный CUDA-билд:
    pip install torch --index-url https://download.pytorch.org/whl/cpu
"""

import io
import logging
import traceback

from PIL import Image

log = logging.getLogger("antispam")

_model = None
_processor = None
_loaded = False
load_error = ""   # текст ошибки последней попытки загрузки (для /diag)

# Описания «плохого» контента (что ловим) и «нормального» (фон).
BAD_PROMPTS = [
    "a graphic photo with a lot of blood and gore",
    "a photo of a dead human body or a corpse",
    "a photo of a dismembered mutilated body",
    "a brutal bloody injury or wound",
    "a violent gory scene",
]
SAFE_PROMPTS = [
    "a normal safe everyday photo",
    "a photo of people, food or animals",
    "a landscape, object or screenshot",
    "a meme or a picture with text",
    "a nude or explicit adult photo",      # decoy: нагота -> сюда, не в «гор»
    "a pornographic sexual image",         # decoy
    "a portrait or selfie of a person",
    "a drawing, cartoon or anime",
]


def load(model_name: str = "openai/clip-vit-base-patch32") -> None:
    """Поднять CLIP (один раз). Тихо отключается, если зависимостей нет."""
    global _model, _processor, _loaded, load_error
    try:
        import torch  # noqa: F401
        from transformers import CLIPModel, CLIPProcessor
        _model = CLIPModel.from_pretrained(model_name)
        _model.eval()
        _processor = CLIPProcessor.from_pretrained(model_name)
        _loaded = True
        load_error = ""
        log.info("Gore-детектор (CLIP) загружен: %s", model_name)
    except Exception as e:
        load_error = f"{type(e).__name__}: {e}"
        log.warning("Gore-детектор НЕ загрузился: %s", load_error)
        log.debug("gore load traceback:\n%s", traceback.format_exc())


def status() -> str:
    """Короткий статус для диагностики."""
    if _loaded:
        return "✅ загружен"
    return f"❌ не загружен ({load_error})" if load_error else "❌ выключен"


def available() -> bool:
    return _loaded


MAX_BYTES = 12 * 1024 * 1024  # очень большие картинки не гоняем (CPU медленный)


def detect(data: bytes, threshold: float):
    """Синхронно (звать через asyncio.to_thread). (метка, вероятность) или None."""
    if not _loaded or len(data) > MAX_BYTES:
        return None
    try:
        import torch
        img = Image.open(io.BytesIO(data)).convert("RGB")
        prompts = BAD_PROMPTS + SAFE_PROMPTS
        inputs = _processor(text=prompts, images=img, return_tensors="pt", padding=True)
        with torch.no_grad():
            probs = _model(**inputs).logits_per_image.softmax(dim=1)[0]
        nbad = len(BAD_PROMPTS)
        bad_p = float(probs[:nbad].sum())
        if bad_p >= threshold:
            bi = int(torch.argmax(probs[:nbad]))
            return BAD_PROMPTS[bi], bad_p
        return None
    except Exception as e:
        log.debug("gore detect fail: %s", e)
        return None
