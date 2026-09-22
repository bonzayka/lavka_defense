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
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Разрешить «dev»-вход без Telegram (для локальной отладки в браузере).
WEBAPP_DEV = getattr(config, "WEBAPP_DEV", None)
if WEBAPP_DEV is None:
    WEBAPP_DEV = os.environ.get("WEBAPP_DEV", "0") not in ("0", "false", "False", "")
# Максимальный возраст initData (секунды) — защита от переигрывания старой ссылки.
INITDATA_MAX_AGE = int(os.environ.get("WEBAPP_INITDATA_MAX_AGE", "86400"))
# Пауза между раздачами (сек), как в групповом боте.
NEXT_HAND_DELAY = 4

# Кеш аватаров пользователей (user_id -> photo_url) с ограничением размера
MAX_AVATARS = 2000
avatar_cache: dict[int, str] = {}


def set_avatar_cache(user_id: int, url: str) -> None:
    if len(avatar_cache) >= MAX_AVATARS and user_id not in avatar_cache:
        try:
            avatar_cache.pop(next(iter(avatar_cache)), None)
        except StopIteration:
            pass
    avatar_cache[user_id] = url


app = FastAPI(title="Poker Mini App")
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ============================ Проверка доступа к чату =======================
async def is_user_in_chat(chat_id: int, user_id: int) -> bool:
    """
    Проверка, состоит ли пользователь в Telegram-чате, к которому привязан стол.
    Если пользователь не состоит (left / kicked), доступ к столу запрещается.
    """
    if WEBAPP_DEV or chat_id > 0:
        return True
    code = f"chat_{chat_id}"
    r = rooms.get(code)
    if r and r.table:
        if user_id in r.table.get("players", {}) or user_id == r.table.get("host"):
            return True
    try:
        import bot
        bot_inst = getattr(bot, "bot", None)
        if not bot_inst:
            return True
        member = await bot_inst.get_chat_member(chat_id, user_id)
        status = getattr(member, "status", None)
        return status not in ("left", "kicked", None)
    except Exception:
        return True


# ============================ Проверка initData =============================
def verify_init_data(init_data: str, bot_token: str, max_age: int = INITDATA_MAX_AGE) -> dict | None:
    """
    Валидация Telegram WebApp initData по официальной схеме:
        secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
        hash   = HMAC_SHA256(key=secret, msg=data_check_string)
    Возвращает dict пользователя {id, name, photo_url} или None, если подпись неверна.
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
    photo_url = user.get("photo_url") or ""
    return {"id": int(uid), "name": name, "photo_url": photo_url}


# =============================== Комнаты/столы ==============================
class Room:
    def __init__(self, code: str, host_id: int, table: dict | None = None,
                 on_action = None, chat_id: int | None = None):
        self.code = code
        self.table = table if table is not None else holdem.new_table(host_id=host_id)
        self.table["code"] = code
        self.conns: dict[WebSocket, int] = {}   # соединение -> uid
        self.lock = asyncio.Lock()
        self.on_action = on_action
        self.last_active = time.time()

    def add_conn(self, ws: WebSocket, uid: int):
        self.conns[ws] = uid
        self.last_active = time.time()

    def drop_conn(self, ws: WebSocket):
        self.conns.pop(ws, None)
        self.last_active = time.time()


rooms: dict[str, Room] = {}


def get_room(code: str, host_id: int) -> Room:
    # Очистка заброшенных пустых комнат (старше 2 часов), чтобы не расходовать память
    if len(rooms) > 50:
        now_ts = time.time()
        for c, r in list(rooms.items()):
            if not c.startswith("chat_") and not r.conns and (now_ts - getattr(r, "last_active", now_ts)) > 7200:
                rooms.pop(c, None)

    room = rooms.get(code)
    if room is None:
        if code.startswith("chat_"):
            try:
                cid = int(code[5:])
                import bot
                game = getattr(bot, "holdem_games", {}).get(cid)
                if game and "table" in game:
                    return bind_chat_table(cid, game["table"], getattr(bot, "_on_webapp_action", None))
            except Exception:
                pass
        room = Room(code, host_id)
        rooms[code] = room
    return room


def bind_chat_table(chat_id: int, table: dict, on_action=None) -> Room:
    """Привязать живой стол из группы Telegram к веб-комнате chat_{chat_id}."""
    code = f"chat_{chat_id}"
    table["code"] = code
    room = rooms.get(code)
    if room is None:
        room = Room(code, host_id=table.get("host", 0), table=table, on_action=on_action, chat_id=chat_id)
        rooms[code] = room
    else:
        room.table = table
        room.on_action = on_action
        room.chat_id = chat_id
    return room


def unbind_chat_table(chat_id: int):
    """Отвязать стол при завершении игры в группе."""
    code = f"chat_{chat_id}"
    rooms.pop(code, None)


def notify_chat_update(chat_id: int):
    """Синхронизировать обновление из Telegram чата во все открытые WebApp стола."""
    code = f"chat_{chat_id}"
    room = rooms.get(code)
    if room and room.conns:
        try:
            asyncio.create_task(broadcast(room))
        except Exception:
            pass


# ============================ Сериализация вида =============================
def _card_list(cards: list[str]) -> list[str]:
    return [holdem.format_card(c) for c in (cards or [])]


def serialize(table: dict, viewer_uid: int, room: Room | None = None) -> dict:
    """Состояние стола глазами конкретного игрока (чужие карты скрыты)."""
    players = table["players"]
    seats = []
    is_round_over = table.get("phase") in ("between_hands", "finished", "closed")
    for uid in table["seats"]:
        p = players[uid]
        is_me = uid == viewer_uid
        # Физическая изоляция: свои карты видны только самому себе (is_me) и в state.you.hole.
        # Для чужих мест реальные карты открываются ТОЛЬКО на шоудауне (is_round_over).
        show_cards = is_me or (is_round_over and bool(p.get("hole")) and not p.get("folded"))
        p_stack = p.get("stack", 0)
        p_bet = 0 if (is_round_over or p_stack <= 0) else p.get("street_bet", 0)
        seats.append({
            "uid": uid,
            "name": p["name"],
            "avatar": p.get("avatar") or avatar_cache.get(uid, ""),
            "stack": p_stack,
            "bet": p_bet,
            "folded": p.get("folded", False),
            "all_in": p.get("all_in", False),
            "in_table": p.get("in_table", False),
            "is_turn": table.get("current_turn") == uid,
            "is_me": is_me,
            "is_host": uid == table.get("host"),
            "misses": p.get("misses", 0),
            "last_action": p.get("last_action", ""),
            "showdown": p.get("showdown_name", ""),
            "cards": _card_list(p.get("hole")) if show_cards else (["🂠", "🂠"] if (p.get("hole") and not p.get("folded")) else []),
        })

    me = players.get(viewer_uid)
    opts = holdem.allowed_actions(table, viewer_uid) if me else {}
    combo_name = ""
    if me and me.get("hole"):
        try:
            combo_name = holdem.eval_player_combination(me.get("hole", []), table.get("board", []))
        except Exception:
            pass

    dealer_idx = table.get("dealer_index", -1)
    seats_list = table.get("seats", [])
    dealer_uid = seats_list[dealer_idx] if 0 <= dealer_idx < len(seats_list) else None

    t_start = table.get("turn_start_time", 0)
    now_t = time.time()
    t_left = max(0, int(holdem.TURN_TIMEOUT_SEC - (now_t - t_start))) if (table.get("phase") == "playing" and table.get("current_turn") and t_start) else 0

    spectators_count = 0
    if room:
        seated_uids = set(table.get("players", {}).keys())
        spectators_count = sum(1 for u in set(room.conns.values()) if u not in seated_uids)

    return {
        "type": "state",
        "code": table.get("code", ""),
        "chat_title": table.get("chat_title", ""),
        "chat_id": table.get("chat_id"),
        "phase": table.get("phase"),
        "street": holdem.street_name(table.get("street")),
        "board": _card_list(table.get("board")),
        "pot": table.get("pot", 0),
        "hand_no": table.get("hand_no", 0),
        "dealer_uid": dealer_uid,
        "small_blind": table.get("small_blind", holdem.SMALL_BLIND),
        "big_blind": table.get("big_blind", holdem.BIG_BLIND),
        "min_raise": table.get("min_raise", holdem.BIG_BLIND),
        "current_turn": table.get("current_turn"),
        "current_bet": table.get("current_bet", 0),
        "turn_timeout_sec": holdem.TURN_TIMEOUT_SEC,
        "turn_seconds_left": t_left,
        "last_event": table.get("last_event", ""),
        "last_payouts": table.get("last_payouts", {}),
        "min_players": holdem.MIN_PLAYERS,
        "is_host": viewer_uid == table.get("host"),
        "spectators_count": spectators_count,
        "seats": seats,
        "you": {
            "uid": viewer_uid,
            "seated": bool(me),
            "hole": _card_list(me.get("hole")) if me else [],
            "combo": combo_name,
            "to_call": holdem.player_to_call(table, viewer_uid) if me else 0,
            "options": opts,
        },
    }


async def broadcast(room: Room):
    """Разослать актуальное состояние всем соединениям (каждому — свой вид)."""
    dead = []
    for ws, uid in list(room.conns.items()):
        try:
            await ws.send_json(serialize(room.table, uid, room))
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


@app.get("/api/rooms")
async def list_rooms():
    active = []
    for code, room in list(rooms.items()):
        tbl = room.table
        active.append({
            "code": code,
            "chat_title": tbl.get("chat_title") or ("Стол чата" if code.startswith("chat_") else "Публичный стол"),
            "phase": tbl.get("phase", "lobby"),
            "players_count": len(tbl.get("seats", [])),
            "pot": tbl.get("pot", 0),
            "hand_no": tbl.get("hand_no", 0),
        })
    return {"ok": True, "rooms": active}


@app.get("/api/avatar/{user_id}")
async def get_avatar(user_id: int):
    """Отдать аватар пользователя Telegram (из кеша или через Bot API)."""
    if user_id in avatar_cache and avatar_cache[user_id]:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(avatar_cache[user_id])
    try:
        import bot
        bot_inst = getattr(bot, "bot", None)
        token = getattr(bot, "BOT_TOKEN", BOT_TOKEN)
        if bot_inst and token:
            photos = await bot_inst.get_user_profile_photos(user_id, limit=1)
            if photos and photos.total_count > 0:
                file_id = photos.photos[0][-1].file_id
                f = await bot_inst.get_file(file_id)
                if f.file_path:
                    url = f"https://api.telegram.org/file/bot{token}/{f.file_path}"
                    set_avatar_cache(user_id, url)
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse(url)
    except Exception:
        pass
    from fastapi.responses import Response
    return Response(status_code=404)


NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE_HEADERS)


@app.get("/blackjack")
async def blackjack():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE_HEADERS)


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
        if user is None and (WEBAPP_DEV or os.environ.get("WEBAPP_DEV", "0") not in ("0", "false", "False", "") or msg.get("dev_uid")):
            duid = int(msg.get("dev_uid") or 1000 + int(time.time()) % 9000)
            user = {"id": duid, "name": msg.get("dev_name") or f"Dev{duid}", "photo_url": ""}
        if user is None:
            await ws.send_json({"type": "error", "error": "bad_init_data"})
            await ws.close()
            return

        uid, name = user["id"], user["name"]
        photo_url = user.get("photo_url") or ""
        if photo_url:
            set_avatar_cache(uid, photo_url)

        # Защита приватности: если комната привязана к чату, проверяем членство
        if code.startswith("chat_"):
            try:
                cid = int(code[5:])
                in_chat = await is_user_in_chat(cid, uid)
                if not in_chat:
                    await ws.send_json({
                        "type": "error",
                        "error": "not_in_chat",
                        "message": "🔒 Доступ ограничен: эта игра проводится в закрытом чате. Вы должны быть участником чата, чтобы присоединиться."
                    })
                    await ws.close()
                    return
            except ValueError:
                pass

        room = get_room(code, host_id=uid)
        room.table["code"] = code
        room.add_conn(ws, uid)
        # Обновим имя и аватар, если игрок уже сидит.
        if uid in room.table["players"]:
            room.table["players"][uid]["name"] = name
            if photo_url:
                room.table["players"][uid]["avatar"] = photo_url
        await ws.send_json(serialize(room.table, uid, room))

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
                    if uid in table.get("players", {}) and avatar_cache.get(uid):
                        table["players"][uid]["avatar"] = avatar_cache[uid]
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
                elif mtype in ("close_table", "cancel"):
                    is_host = (uid == table.get("host"))
                    is_fin = (table.get("phase") in ("finished", "lobby", "closed"))
                    if is_host or is_fin:
                        table["phase"] = "closed"
                        table["last_event"] = f"🛑 Стол закрыт ({name})."
                elif mtype == "ping":
                    pass

            await broadcast(room)
            # Для комнат Telegram-чата раздачу ведёт бот (_holdem_after_action).
            # maybe_next_hand запускается только для автономных веб-комнат без chat_id.
            if follow_up and not (room and room.chat_id):
                asyncio.create_task(maybe_next_hand(room))

            # Если комната привязана к чату Telegram — синхронизируем изменения с группой!
            if room and room.on_action and room.chat_id:
                try:
                    res = room.on_action(room.chat_id, mtype, uid)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception:
                    pass


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


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
        return response


# Статика с кэшированием (CSS, JS кэшируются браузером на 7 дней).
if STATIC_DIR.exists():
    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")


# ===================== Запуск внутри процесса бота ==========================
async def run_in_background(host: str = "0.0.0.0", port: int = 3000):
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
        port=int(os.environ.get("PORT", os.environ.get("WEBAPP_PORT", "3000"))),
        reload=bool(os.environ.get("WEBAPP_RELOAD")),
    )

