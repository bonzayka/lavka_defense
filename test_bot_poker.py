"""Test real poker handlers in isolation without starting the bot or loading its DB."""
import ast
import asyncio
import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import holdem


def handlers():
    names = {"holdem_lobby_cb", "_holdem_after_action", "_holdem_send_turn_prompt", "_holdem_turn_timer"}
    source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
    functions = [n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    for function in functions:
        function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(module)
    env = {
        "holdem": holdem, "asyncio": asyncio, "time": time,
        "log": logging.getLogger(__name__), "holdem_games": {},
        "holdem_custom_wait": {}, "holdem_turn_tasks": {},
        "_fmt_chips": str, "_holdem_turn_kb": Mock(return_value=None),
    }
    for name in ("_holdem_dm", "_holdem_cancel_timer", "_holdem_refresh",
                 "_holdem_announce_hole_cards", "_holdem_finish_if_needed", "_holdem_timeout_apply"):
        env[name] = AsyncMock()
    exec(compile(module, "bot.py", "exec"), env)
    return env


class BotPokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = handlers()
        self.table = holdem.new_table(1)
        for uid in (1, 2):
            holdem.add_player(self.table, uid, str(uid))
        self.env["holdem_games"][7] = {"table": self.table}

    async def test_only_host_can_start_or_close(self):
        for action in ("start", "cancel"):
            cb = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=7)),
                                 from_user=SimpleNamespace(id=2), data=f"th:{action}", answer=AsyncMock())
            await self.env["holdem_lobby_cb"](cb)
            self.assertEqual(self.table["phase"], "lobby")
            self.assertTrue(cb.answer.call_args.kwargs["show_alert"])

    async def test_prompt_preserves_engine_token(self):
        holdem.start_tournament(self.table, seed=1)
        token = self.table["turn_token"]
        await self.env["_holdem_send_turn_prompt"](7)
        self.assertEqual(self.table["turn_token"], token)
        task = self.env["holdem_turn_tasks"][7]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_prompt_cannot_replace_new_timer(self):
        holdem.start_tournament(self.table, seed=1)

        async def delayed_dm(*args):
            holdem.apply_action(self.table, self.table["current_turn"], "call")

        self.env["_holdem_dm"].side_effect = delayed_dm
        await self.env["_holdem_send_turn_prompt"](7)
        self.env["_holdem_cancel_timer"].assert_not_awaited()
        self.assertFalse(self.env["holdem_turn_tasks"])

    async def test_immediate_runouts_keep_advancing(self):
        self.table.update(phase="between_hands", hand_no=1)

        def begin(t):
            t["hand_no"] += 1
            if t["hand_no"] == 4:
                t["phase"] = "finished"

        with patch.object(asyncio, "sleep", new=AsyncMock()), patch.object(holdem, "begin_hand", side_effect=begin):
            await self.env["_holdem_after_action"](7)
        self.assertEqual(self.table["hand_no"], 4)
        self.env["_holdem_finish_if_needed"].assert_awaited_once_with(7)

    async def test_expired_timer_uses_remaining_time(self):
        holdem.start_tournament(self.table, seed=1)
        self.table["turn_start_time"] = time.time() - holdem.TURN_TIMEOUT_SEC - 1
        with patch.object(asyncio, "sleep", new=AsyncMock()) as sleep:
            await self.env["_holdem_turn_timer"](7, self.table["turn_token"], self.table["current_turn"])
        sleep.assert_awaited_once_with(0)
        self.env["_holdem_timeout_apply"].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
