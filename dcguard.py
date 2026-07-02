# -*- coding: utf-8 -*-
"""
Определение датацентра (DC) пользователя по его профильному фото — через
Bot API. DC закодирован внутри file_id фотографии (offset 4, little-endian
uint32, сразу после file_type). DC1..DC5; DC5 = Singapore — часто у ботов.

ВАЖНО (честно): это ЭВРИСТИКА. DC5 — это и реальные пользователи из ЮВА.
Будут ложные срабатывания. Используй осознанно (лучше точечно при рейде).
Если у юзера скрыто фото приватностью — DC не определить (вернёт None).
"""

import base64
import struct


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _rle_decode(data: bytes) -> bytes:
    """Telegram кодирует серии нулей: 0x00, затем байт-счётчик нулей."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0 and i + 1 < n:
            out.extend(b"\x00" * data[i + 1])
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def dc_from_file_id(file_id: str) -> int | None:
    """Вернуть DC (1..5) из file_id фотографии или None."""
    try:
        data = _rle_decode(_b64(file_id))
        if len(data) < 8:
            return None
        _file_type, dc_id = struct.unpack("<II", data[:8])
        return dc_id if 1 <= dc_id <= 5 else None
    except Exception:
        return None
