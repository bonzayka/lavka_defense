# -*- coding: utf-8 -*-
"""
Асинхронный клиент для взаимодействия с микросервисом инференса Lavka Defense.
Поддерживает Unix Domain Socket (Linux) и TCP loopback (Windows).
Обеспечивает автозапуск микросервиса (если не поднят), пул соединений, таймауты
и корректное завершение.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time

import aiohttp

import config

log = logging.getLogger("inference_client")

_client_instance: "InferenceClient | None" = None


class InferenceClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._service_proc: subprocess.Popen | None = None
        self._is_started_by_us: bool = False
        self._target: str = self._resolve_target()
        self._is_unix: bool = self._target.startswith("/") or self._target.endswith(".sock")
        self._base_url: str = "http://localhost" if self._is_unix else self._target.rstrip("/")

    @staticmethod
    def _resolve_target() -> str:
        t = os.environ.get("INFERENCE_SOCKET") or getattr(config, "INFERENCE_SOCKET", None)
        if not t:
            if sys.platform != "win32":
                return "/tmp/defense_inference.sock"
            return "http://127.0.0.1:8765"
        return t

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=getattr(config, "INFERENCE_TIMEOUT", 60.0))
            if self._is_unix:
                connector = aiohttp.UnixConnector(path=self._target)
            else:
                connector = aiohttp.TCPConnector()
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def is_healthy(self) -> bool:
        """Быстрая проверка доступности сервиса."""
        try:
            sess = await self._get_session()
            async with sess.get(f"{self._base_url}/health", timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "ok"
                return False
        except Exception:
            return False

    async def get_status(self) -> dict | None:
        """Получить статус сервиса и моделей."""
        try:
            sess = await self._get_session()
            async with sess.get(f"{self._base_url}/status", timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            log.debug("Ошибка получения статуса инференса: %s", e)
            return None

    async def ensure_service_running(self) -> bool:
        """
        Проверяет, запущен ли микросервис. Если нет и включён INFERENCE_AUTO_START,
        запускает его как отдельный фоновый процесс и ожидает готовности.
        """
        mode = getattr(config, "INFERENCE_MODE", "microservice")
        if mode != "microservice":
            return False

        if await self.is_healthy():
            log.info("Микросервис инференса уже запущен на %s", self._target)
            return True

        auto_start = getattr(config, "INFERENCE_AUTO_START", True)
        if not auto_start:
            log.warning("Микросервис инференса недоступен на %s, а INFERENCE_AUTO_START=False.", self._target)
            return False

        log.info("Автозапуск микросервиса инференса (%s)...", self._target)
        service_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inference_service.py")
        env = dict(os.environ)
        env["INFERENCE_SOCKET"] = self._target

        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP = 0x00000200
            creationflags = 0x00000200

        try:
            self._service_proc = subprocess.Popen(
                [sys.executable, service_script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._is_started_by_us = True
        except Exception as e:
            log.error("Не удалось запустить микросервис инференса: %s", e)
            return False

        # Ожидание готовности до 15 секунд
        deadline = time.time() + 15.0
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            if self._service_proc.poll() is not None:
                log.error("Микросервис инференса завершился с кодом %d сразу после старта.", self._service_proc.poll())
                return False
            if await self.is_healthy():
                log.info("Микросервис инференса успешно запущен и готов к работе (PID=%d).", self._service_proc.pid)
                return True

        log.error("Таймаут ожидания старта микросервиса инференса (%s).", self._target)
        return False

    def stop_service(self) -> None:
        """Остановить процесс микросервиса, если он был поднят нами."""
        if self._is_started_by_us and self._service_proc and self._service_proc.poll() is None:
            log.info("Остановка дочернего процесса микросервиса инференса (PID=%d)...", self._service_proc.pid)
            try:
                self._service_proc.terminate()
                self._service_proc.wait(timeout=5)
            except Exception:
                try:
                    self._service_proc.kill()
                except Exception:
                    pass
            self._service_proc = None
            self._is_started_by_us = False

    async def detect_nsfw(self, data: bytes) -> float | None:
        """Запрос классификации 18+ (ViT). Возвращает вероятность или None."""
        if not data:
            return None
        try:
            sess = await self._get_session()
            headers = {"Content-Type": "application/octet-stream"}
            async with sess.post(f"{self._base_url}/nsfw", data=data, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res.get("prob")
                return None
        except Exception as e:
            log.warning("Inference IPC (nsfw) fail: %s", e)
            return None

    async def detect_gore(self, data: bytes, threshold: float = 0.6) -> tuple[str, float] | None:
        """Запрос детекции шок-контента/гора (CLIP)."""
        if not data:
            return None
        try:
            sess = await self._get_session()
            headers = {"Content-Type": "application/octet-stream"}
            params = {"threshold": str(threshold)}
            async with sess.post(f"{self._base_url}/gore", data=data, headers=headers, params=params) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    if res.get("hit"):
                        return (res["label"], float(res["prob"]))
                return None
        except Exception as e:
            log.warning("Inference IPC (gore) fail: %s", e)
            return None

    async def extract_ocr_text(self, data: bytes, lang: str = "rus+eng") -> str:
        """Запрос OCR распознавания текста на картинке."""
        if not data:
            return ""
        try:
            sess = await self._get_session()
            headers = {"Content-Type": "application/octet-stream"}
            params = {"lang": lang}
            async with sess.post(f"{self._base_url}/ocr", data=data, headers=headers, params=params) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res.get("text", "")
                return ""
        except Exception as e:
            log.warning("Inference IPC (ocr) fail: %s", e)
            return ""

    async def transcribe_audio(self, data: bytes, ext: str = "ogg", language: str = "ru-RU") -> str | None:
        """Запрос транскрибации аудио/видеосообщения."""
        if not data:
            return ""
        try:
            sess = await self._get_session()
            headers = {"Content-Type": "application/octet-stream"}
            params = {"ext": ext, "language": language}
            async with sess.post(f"{self._base_url}/transcribe", data=data, headers=headers, params=params) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res.get("text")
                return None
        except Exception as e:
            log.warning("Inference IPC (transcribe) fail: %s", e)
            return None


def get_client() -> InferenceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = InferenceClient()
    return _client_instance
