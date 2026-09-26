"""Offline regressions: permissions, privacy, malformed requests and timers.

Run: python -m unittest webapp.test_regressions -v
"""
import asyncio
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient

import holdem
from webapp import server as s


def signed(params):
    token = "test:token"
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    payload = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest}), token


def table():
    t = holdem.new_table(1)
    for uid in (1, 2, 3):
        holdem.add_player(t, uid, str(uid))
    holdem.start_tournament(t, seed=42)
    return t


class AuthTests(unittest.TestCase):
    def test_invalid_dates_and_users(self):
        valid = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})}
        self.assertEqual(s.verify_init_data(*signed(valid))["id"], 1)
        for date in ("", "abc", str(int(time.time()) + 600), "1"):
            self.assertIsNone(s.verify_init_data(*signed({**valid, "auth_date": date})))
        for user in ([], None, {"id": True}, {"id": "x"}, {"id": -2}):
            self.assertIsNone(s.verify_init_data(*signed({**valid, "user": json.dumps(user)})))

    def test_dev_uid_cannot_bypass_production_auth(self):
        with patch.object(s, "WEBAPP_DEV", False), TestClient(s.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth", "dev_uid": 1, "room": "test"})
                self.assertEqual(ws.receive_json()["error"], "bad_init_data")


class ViewTests(unittest.TestCase):
    def test_uncontested_winner_keeps_cards_private(self):
        t = table()
        holdem.apply_action(t, 1, "fold")
        holdem.apply_action(t, 2, "fold")
        view = s.serialize(t, 999)
        self.assertTrue(all(c == "🂠" for p in view["seats"] for c in p["cards"]))

    def test_allin_is_live_and_bet_visible(self):
        t = table()
        holdem.apply_action(t, 1, "allin")
        seat = s.serialize(t, 2)["seats"][0]
        self.assertTrue(seat["in_table"] and seat["all_in"])
        self.assertEqual(seat["bet"], 50000)
        self.assertEqual(seat["cards"], ["🂠", "🂠"])

    def test_showdown_reveals_nonfolded_hands(self):
        t = table()
        while t["phase"] == "playing":
            uid = t["current_turn"]
            opts = holdem.allowed_actions(t, uid)
            self.assertTrue(holdem.apply_action(t, uid, "allin" if opts["all_in"] else "call")["ok"])
        for seat in s.serialize(t, 999)["seats"]:
            self.assertEqual(len(seat["cards"]), 2)
            self.assertNotIn("🂠", seat["cards"])


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.rooms_patch = patch.object(s, "rooms", {})
        self.dev_patch = patch.object(s, "WEBAPP_DEV", True)
        self.rooms_patch.start()
        self.dev_patch.start()
        self.addCleanup(self.rooms_patch.stop)
        self.addCleanup(self.dev_patch.stop)

    def auth(self, ws, uid=1):
        ws.send_json({"type": "auth", "dev_uid": uid, "room": "regression"})
        return ws.receive_json()

    def test_spectator_cannot_manage_table(self):
        s.rooms["regression"] = s.Room("regression", 1)
        with TestClient(s.app) as client, client.websocket_connect("/ws") as ws:
            self.auth(ws, 2)
            for action in ("start", "next_hand", "cancel", "close_table"):
                ws.send_json({"type": action})
                self.assertEqual(ws.receive_json()["error"], "host_only")
                self.assertEqual(s.rooms["regression"].table["phase"], "lobby")

    def test_errors_are_reported_and_connection_survives(self):
        with TestClient(s.app) as client, client.websocket_connect("/ws") as ws:
            self.auth(ws)
            for raw in ("[1]", "null", "{broken"):
                ws.send_text(raw)
                self.assertEqual(ws.receive_json()["error"], "invalid_message")
            ws.send_json({"type": "start"})
            self.assertEqual(ws.receive_json()["error"], "not_enough_players")
            self.assertEqual(ws.receive_json()["phase"], "lobby")
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json()["type"], "pong")

    def test_stale_action_does_not_change_chips(self):
        room = s.Room("regression", 1, table=table())
        s.rooms[room.code] = room
        before = room.table["pot"]
        with TestClient(s.app) as client, client.websocket_connect("/ws") as ws:
            view = self.auth(ws)
            ws.send_json({"type": "action", "action": "allin", "turn_token": view["turn_token"] - 1})
            self.assertEqual(ws.receive_json()["error"], "stale_turn")
            self.assertEqual(ws.receive_json()["pot"], before)

    def test_private_rooms_not_listed_publicly(self):
        s.rooms["chat_-1"] = s.Room("chat_-1", 1)
        s.rooms["public"] = s.Room("public", 1)
        with TestClient(s.app) as client:
            self.assertEqual([r["code"] for r in client.get("/api/rooms").json()["rooms"]], ["public"])

    def test_avatar_never_redirects_to_bot_token(self):
        with patch.dict(s.avatar_cache, {1: "https://api.telegram.org/file/botSECRET/photo.jpg"}):
            with TestClient(s.app) as client:
                response = client.get("/api/avatar/1", follow_redirects=False)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("location", response.headers)


class TimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_advances_turn_and_preserves_chips(self):
        room = s.Room("timer", 1, table=table())
        room.table["turn_start_time"] = 0
        uid = room.table["current_turn"]
        with patch.object(room, "schedule_turn"):
            await s.expire_turn(room, room.table["turn_token"])
        self.assertTrue(room.table["players"][uid]["folded"])
        self.assertEqual(room.table["players"][uid]["misses"], 1)
        self.assertNotEqual(room.table["current_turn"], uid)
        self.assertEqual(sum(p["stack"] for p in room.table["players"].values()) + room.table["pot"], 150000)

    async def test_old_timer_cannot_act_on_new_turn(self):
        room = s.Room("timer", 1, table=table())
        room.table["turn_start_time"] = 0
        token = room.table["turn_token"]
        holdem.apply_action(room.table, room.table["current_turn"], "call")
        room.table["turn_start_time"] = 0
        uid = room.table["current_turn"]
        await s.expire_turn(room, token)
        self.assertEqual(room.table["current_turn"], uid)
        self.assertEqual(room.table["players"][uid]["misses"], 0)

    async def test_next_hand_chains_immediate_runouts(self):
        room = s.Room("timer", 1)
        room.table.update(phase="between_hands", hand_no=1)

        def begin(t):
            t["hand_no"] += 1
            if t["hand_no"] == 4:
                t["phase"] = "finished"

        with patch.object(s, "NEXT_HAND_DELAY", 0), patch.object(holdem, "begin_hand", side_effect=begin):
            await asyncio.wait_for(s.maybe_next_hand(room), 1)
        self.assertEqual(room.table["hand_no"], 4)

    async def test_chat_room_does_not_start_competing_timers(self):
        room = s.Room("chat_-1", 1, table=table(), chat_id=-1)
        room.schedule_turn()
        room.schedule_next_hand()
        self.assertIsNone(room.turn_task)
        self.assertIsNone(room.next_hand_task)


if __name__ == "__main__":
    unittest.main()
