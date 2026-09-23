# -*- coding: utf-8 -*-
"""
Изолированный микросервис инференса для Lavka Defense.
Выносит тяжёлые библиотеки (onnxruntime, PyTorch, RapidOCR, Faster-Whisper)
в отдельный системный процесс с пониженным приоритетом планировщика ОС.
Общается с ботом через Unix Domain Socket (Linux) или TCP loopback (Windows).
"""

import asyncio
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
import uvicorn

import config
import deanon
import gore
import nsfwvit
import voiceguard

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | INFERENCE | %(levelname)s | %(message)s"
)
log = logging.getLogger("inference")

START_TIME = time.time()


def set_low_process_priority() -> None:
    """Установить процессу пониженный приоритет CPU (nice / below normal)."""
    # Linux / macOS / Unix
    if hasattr(os, "nice"):
        try:
            os.nice(10)
            log.info("Установлен nice +10 для приоритета CPU.")
        except Exception as e:
            log.debug("Не удалось вызвать os.nice: %s", e)

    # Windows
    if sys.platform == "win32":
        try:
            import ctypes
            # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000
            )
            log.info("Установлен BELOW_NORMAL_PRIORITY_CLASS для процесса Windows.")
        except Exception as e:
            log.debug("Не удалось установить приоритет на Windows: %s", e)


def tune_cpu_threads() -> None:
    """Ограничение потоков для OpenMP, PyTorch и ONNX Runtime."""
    threads = int(os.environ.get("INFERENCE_THREADS", getattr(config, "INFERENCE_THREADS", 0)))
    if threads > 0:
        os.environ["OMP_NUM_THREADS"] = str(threads)
        os.environ["MKL_NUM_THREADS"] = str(threads)
        try:
            import torch
            torch.set_num_threads(threads)
            log.info("PyTorch ограничен %d потоками CPU.", threads)
        except ImportError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_low_process_priority()
    tune_cpu_threads()

    # Предзагрузка моделей по конфигурации
    if getattr(config, "NSFW_ENABLED", True):
        log.info("Предзагрузка ViT 18+ классификатора...")
        nsfwvit.load(getattr(config, "NSFW_MODEL", "full"), getattr(config, "NSFW_THREADS", 0))

    if getattr(config, "GORE_ENABLED", True):
        log.info("Предзагрузка CLIP gore-детектора...")
        gore.load(getattr(config, "GORE_MODEL", "openai/clip-vit-base-patch32"))

    if getattr(config, "DEANON_ENABLED", True):
        log.info("Предзагрузка OCR детектора...")
        deanon.load(getattr(config, "DEANON_OCR_LANG", "rus+eng"))

    log.info("Микросервис инференса готов к обработке запросов.")
    yield
    log.info("Микросервис инференса завершает работу.")


app = FastAPI(title="Lavka Defense Inference Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "pid": os.getpid(),
        "uptime_sec": round(time.time() - START_TIME, 2),
    }


@app.get("/status")
async def status():
    return {
        "status": "ok",
        "pid": os.getpid(),
        "uptime_sec": round(time.time() - START_TIME, 2),
        "models": {
            "nsfw": {
                "available": nsfwvit.available(),
                "status": nsfwvit.status(),
            },
            "gore": {
                "available": gore.available(),
                "status": gore.status(),
            },
            "ocr": {
                "available": deanon.available(),
                "status": deanon.status(),
            },
            "voice": {
                "available": voiceguard.available(),
                "has_whisper": voiceguard.has_whisper(),
                "has_ffmpeg": voiceguard.has_ffmpeg(),
            },
        },
    }


@app.post("/nsfw")
async def detect_nsfw(request: Request):
    """Классификация 18+ (ViT). Возвращает {'prob': float | None}."""
    data = await request.body()
    if not data:
        return {"prob": None}
    if not nsfwvit.available():
        nsfwvit.load(getattr(config, "NSFW_MODEL", "full"), getattr(config, "NSFW_THREADS", 0))

    prob = await asyncio.to_thread(nsfwvit.detect_prob, data)
    return {"prob": prob}


@app.post("/gore")
async def detect_gore(request: Request, threshold: float = Query(0.6)):
    """Детекция шок-контента/гора (CLIP)."""
    data = await request.body()
    if not data:
        return {"hit": False, "label": None, "prob": None}
    if not gore.available():
        gore.load(getattr(config, "GORE_MODEL", "openai/clip-vit-base-patch32"))

    res = await asyncio.to_thread(gore.detect, data, threshold)
    if res:
        label, prob = res
        return {"hit": True, "label": label, "prob": prob}
    return {"hit": False, "label": None, "prob": None}


@app.post("/ocr")
async def extract_ocr(request: Request, lang: str = Query("rus+eng")):
    """Извлечение текста с картинки (RapidOCR / Tesseract)."""
    data = await request.body()
    if not data:
        return {"text": ""}
    if not deanon.available():
        deanon.load(lang)

    text = await asyncio.to_thread(deanon.extract_text, data, lang)
    return {"text": text or ""}


@app.post("/transcribe")
async def transcribe_audio(
    request: Request,
    ext: str = Query("ogg"),
    language: str = Query("ru-RU")
):
    """Конвертация и расшифровка голосового/видеосообщения (Faster-Whisper / Google)."""
    data = await request.body()
    if not data:
        return {"text": ""}

    if not voiceguard.available():
        raise HTTPException(
            status_code=503,
            detail="Движок распознавания речи (ffmpeg / faster-whisper / speech_recognition) недоступен"
        )

    try:
        text = await asyncio.to_thread(voiceguard._sync_convert_and_recognize, data, ext, language)
        return {"text": text if text is not None else ""}
    except Exception as e:
        log.warning("Ошибка транскрибации: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def run_server():
    raw_socket = os.environ.get("INFERENCE_SOCKET") or getattr(config, "INFERENCE_SOCKET", None)
    if not raw_socket:
        if sys.platform != "win32":
            raw_socket = "/tmp/defense_inference.sock"
        else:
            raw_socket = "http://127.0.0.1:8765"

    log.info("Запуск микросервиса инференса на %s", raw_socket)

    # Запуск на Unix Domain Socket
    if raw_socket.startswith("/") or raw_socket.endswith(".sock"):
        sock_path = raw_socket
        if os.path.exists(sock_path):
            try:
                os.remove(sock_path)
            except OSError:
                pass

        # Настройка Uvicorn на UDS
        server_config = uvicorn.Config(
            app=app,
            uds=sock_path,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(server_config)

        # После создания сокета делаем доступным для всех процессов
        def make_socket_accessible():
            time.sleep(0.5)
            if os.path.exists(sock_path):
                try:
                    os.chmod(sock_path, 0o666)
                except Exception:
                    pass
        import threading
        threading.Thread(target=make_socket_accessible, daemon=True).start()
        server.run()
    else:
        # Запуск на TCP
        cleaned = raw_socket.replace("http://", "").replace("tcp://", "")
        if ":" in cleaned:
            host, port_str = cleaned.split(":", 1)
            port = int(port_str.split("/")[0])
        else:
            host = "127.0.0.1"
            port = 8765

        uvicorn.run(
            app=app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )


if __name__ == "__main__":
    run_server()
