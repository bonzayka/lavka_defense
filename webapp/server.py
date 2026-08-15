# -*- coding: utf-8 -*-
"""
Бэкенд Telegram Mini App для покера (Texas Hold'em).

Что делает:
  • Отдаёт одностраничный веб-стол (static/index.html) с telegram-web-app.js.
  • Держит игровые «комнаты» (столы) в памяти поверх чистой логики holdem.py.
  • Реалтайм через WebSocket: каждый ход рассылается всем за столом, при этом
    каждому игроку видны ТОЛЬКО его карманные карты.
  • Проверяет подпись Telegram (initData) — так мы доверяем uid/имени игрока.

Как поднять локально/на VPS:
    uvicorn webapp.server:app --host 0.0.0.0 --port 8080
или (single-process вместе с ботом) — см. run_in_background() и bot.py.

ВАЖНО: Telegram открывает Mini App только по HTTPS. На VPS поставь reverse-proxy
(nginx/caddy) с TLS на этот порт и укажи публичный https-URL в config.WEBAPP_URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl

# holdem.py лежит в корне проекта — гарантируем, что он импортируется,
# как бы ни запускали uvicorn.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import holdem  # noqa: E402

try:
    import config  # noqa: E402
    BOT_TOKEN = config.BOT_TOKEN
except Exception:  # запуск без config — берём токен из env
    config = None
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Разрешить «dev»-вход без Telegram (для локальной отладки в браузере).
# На проде держи 0 — иначе кто угодно зайдёт под любым uid.
WEBAPP_DEV = os.environ.get("WEBAPP_DEV", "0") not in ("0", "false", "False", "")
# Максимальный возраст initData (секунды) — защита от переигрывания старой ссылки.
INITDATA_MAX_AGE = int(os.environ.get("WEBAPP_INITDATA_MAX_AGE", "86400"))
# Пауза между раздачами (сек), как в групповом боте.
NEXT_HAND_DELAY = 4

app = FastAPI(title="Poker Mini App")


# ============================ Проверка initData =============================
def verify_init_data(init_data: str, bot_token: str, max_age: int = INITDATA_MAX_AGE) -> dict | None:
    """
    Валидация Telegram WebApp initData по официальной схеме:
        secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
        hash   = HMAC_SHA256(key=secret, msg=data_check_string)
    Возвращает dict пользователя {id, name} или None, если подпись неверна.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # Свежесть (не обязательно, но полезно).
    if max_age and pairs.get("auth_date", "").isdigit():
        if time.time() - int(pairs["auth_date"]) > max_age:
            return None

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    uid = user.get("id")
    if not uid:
        return None
    name = (f"{user.get('first_name', '')} {user.get('last_name', '')}").strip() \
        or user.get("username") or f"Игрок {uid}"
    return {"id": int(uid), "name": name}


# =============================== Комнаты/столы ==============================
class Room:
    def __init__(self, code: str, host_id: int):
        self.code = code
        self.table = holdem.new_table(host_id=host_id)
        self.conns: dict[WebSocket, int] = {}   # соединение -> uid
        self.lock = asyncio.Lock()

    def add_conn(self, ws: WebSocket, uid: int):
        self.conns[ws] = uid

    def drop_conn(self, ws: WebSocket):
        self.conns.pop(ws, None)


rooms: dict[str, Room] = {}


def get_room(code: str, host_id: int) -> Room:
    room = rooms.get(code)
    if room is None:
        room = Room(code, host_id)
        rooms[code] = room
    return room


# ============================ Сериализация вида =============================
def _card_list(cards: list[str]) -> list[str]:
    return [holdem.format_card(c) for c in (cards or [])]


def serialize(table: dict, viewer_uid: int) -> dict:
    """Состояние стола глазами конкретного игрока (чужие карты скрыты)."""
    players = table["players"]
    seats = []
    for uid in table["seats"]:
        p = players[uid]
        is_me = uid == viewer_uid
        show_cards = is_me or (table.get("phase") in ("between_hands", "finished")
                               and p.get("hole") and not p.get("folded"))
        seats.append({
            "uid": uid,
            "name": p["name"],
            "stack": p["stack"],
            "bet": p.get("street_bet", 0),
            "folded": p.get("folded", False),
            "all_in": p.get("all_in", False),
            "in_table": p.get("in_table", False),
            "is_turn": table.get("current_turn") == uid,
            "is_me": is_me,
            "last_action": p.get("last_action", ""),
            "showdown": p.get("showdown_name", ""),
            "cards": _card_list(p.get("hole")) if show_cards else (["🂠", "🂠"] if p.get("hole") else []),
        })

    me = players.get(viewer_uid)
    opts = holdem.allowed_actions(table, viewer_uid) if me else {}
    return {
        "type": "state",
        "code": table.get("code", ""),
        "phase": table.get("phase"),
        "street": holdem.street_name(table.get("street")),
        "board": _card_list(table.get("board")),
        "pot": table.get("pot", 0),
        "hand_no": table.get("hand_no", 0),
        "current_turn": table.get("current_turn"),
        "current_bet": table.get("current_bet", 0),
        "last_event": table.get("last_event", ""),
        "min_players": holdem.MIN_PLAYERS,
        "seats": seats,
        "you": {
            "uid": viewer_uid,
            "seated": bool(me),
            "to_call": holdem.player_to_call(table, viewer_uid) if me else 0,
            "options": opts,
        },
    }


async def broadcast(room: Room):
    """Разослать актуальное состояние всем соединениям (каждому — свой вид)."""
    dead = []
    for ws, uid in list(room.conns.items()):
        try:
            await ws.send_json(serialize(room.table, uid))
        except Exception:
            dead.append(ws)
    for ws in dead:
        room.drop_conn(ws)


async def maybe_next_hand(room: Room):
    """Между раздачами: подождать и раздать следующую (как в групповом боте)."""
    table = room.table
    if table.get("phase") != "between_hands":
        return
    await broadcast(room)
    await asyncio.sleep(NEXT_HAND_DELAY)
    async with room.lock:
        if table.get("phase") == "between_hands" and len(holdem.active_table_players(table)) >= 2:
            holdem.begin_hand(table)
    await broadcast(room)


# ================================ HTTP-роуты ================================
@app.get("/health")
async def health():
    return {"ok": True, "rooms": len(rooms)}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ============================== WebSocket-роут ==============================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room: Room | None = None
    uid: int | None = None
    name: str = ""

    try:
        # 1) Первое сообщение — авторизация: {type:"auth", initData, room, dev_uid?}
        raw = await ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            await ws.send_json({"type": "error", "error": "auth_required"})
            await ws.close()
            return

        code = str(msg.get("room") or "main")[:32]
        user = verify_init_data(msg.get("initData", ""), BOT_TOKEN)
        if user is None and WEBAPP_DEV:
            duid = int(msg.get("dev_uid") or 1000 + int(time.time()) % 9000)
            user = {"id": duid, "name": msg.get("dev_name") or f"Dev{duid}"}
        if user is None:
            await ws.send_json({"type": "error", "error": "bad_init_data"})
            await ws.close()
            return

        uid, name = user["id"], user["name"]
        room = get_room(code, host_id=uid)
        room.table["code"] = code
        room.add_conn(ws, uid)
        # Обновим имя, если игрок уже сидит.
        if uid in room.table["players"]:
            room.table["players"][uid]["name"] = name
        await ws.send_json(serialize(room.table, uid))

        # 2) Основной цикл действий.
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")
            follow_up = False

            async with room.lock:
                table = room.table
                if mtype == "join":
                    holdem.add_player(table, uid, name)
                elif mtype == "start":
                    holdem.start_tournament(table)
                elif mtype == "action":
                    action = data.get("action")
                    amount = data.get("amount")
                    try:
                        amount = int(amount) if amount is not None else None
                    except (TypeError, ValueError):
                        amount = None
                    holdem.apply_action(table, uid, action, amount)
                    follow_up = table.get("phase") == "between_hands"
                elif mtype == "ping":
                    pass

            await broadcast(room)
            if follow_up:
                asyncio.create_task(maybe_next_hand(room))

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        if room is not None:
            room.drop_conn(ws)


# Статика (telegram-web-app.js подключаем с CDN в index.html, но папку монтируем).
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===================== Запуск внутри процесса бота ==========================
async def run_in_background(host: str = "0.0.0.0", port: int = 8080):
    """Поднять uvicorn как задачу в уже существующем event loop (вызывается из bot.py)."""
    import uvicorn
    conf = uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(conf)
    await server.serve()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "webapp.server:app",
        host=os.environ.get("WEBAPP_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEBAPP_PORT", "8080")),
        reload=bool(os.environ.get("WEBAPP_RELOAD")),
    )
