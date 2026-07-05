# -*- coding: utf-8 -*-
"""
Набор тестов бота. Запуск:  venv\\Scripts\\python.exe tests.py
Использует временный DATA_FILE, реальные данные не трогает. NudeNet не грузится.
"""

import asyncio
import os
import re
import sys
import tempfile
import types

os.environ["DATA_FILE"] = os.path.join(tempfile.gettempdir(), "defense_test_data.json")
os.environ["CHILDREN_FILE_TEST"] = "1"
os.environ["NSFW_ENABLED"] = "0"
for f in (os.environ["DATA_FILE"], os.environ["DATA_FILE"] + ".tmp"):
    try:
        os.remove(f)
    except OSError:
        pass

import textguard  # noqa: E402
import storage    # noqa: E402
import manager    # noqa: E402
storage.load()
import bot        # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL:", name)


# ---- антимат ----
CLEAN = ["требовать", "хлебать", "погреб", "сгрёб", "сухую", "команда", "мандарин",
         "барсук", "рубля", "сабля", "употреблять", "себя", "закончил", "документ"]
DIRTY = ["хуй", "нахуй", "ахуеть", "пизда", "ебать", "заебал", "долбоёб", "блядь",
         "сука", "мудак", "пидор", "хyй", "х.у.й", "пиzдец", "х у й"]
check("antimat: нет ложных", not any(textguard.has_profanity(w) for w in CLEAN))
check("antimat: ловит мат", all(textguard.has_profanity(w) for w in DIRTY))

# ---- парсер длительности ----
check("dur 3 дня", bot.parse_duration("3 дня") == 259200)
check("dur 2 часа", bot.parse_duration("2 часа") == 7200)
check("dur 30 минут", bot.parse_duration("30 минут") == 1800)
check("dur 1 неделя", bot.parse_duration("1 неделя") == 604800)
check("dur пусто", bot.parse_duration("") is None)

# ---- тайм-команды: ловят команды, игнорят болтовню ----
pat = re.compile(bot.NL_PATTERN)
for s in ["мут 3 дня", "бан", "размут", "варн", "кик", "бан 2 часа"]:
    check(f"nl matches '{s}'", bool(pat.match(s)))
for s in ["я тебе сейчас мут дам", "бан этим спамерам не помешает", "мут ему дай быстро"]:
    check(f"nl ignores '{s}'", not pat.match(s))

# ---- ссылка на сообщение ----
C = types.SimpleNamespace
check("link username", bot.message_link(C(username="g", id=-1001), 5) == "https://t.me/g/5")
check("link private", bot.message_link(C(username=None, id=-1001234567890), 7)
      == "https://t.me/c/1234567890/7")
check("link basic none", bot.message_link(C(username=None, id=-44), 7) is None)

# ---- капча ----
ok = True
for _ in range(100):
    for st in bot.build_questions():
        o = bot.options_for(st)
        if st["answer"] not in o or len(o) != 4 or len(set(o)) != 4:
            ok = False
check("captcha: ответ всегда среди 4 вариантов", ok)

# ---- хранилище: round-trip ----
storage.add_stopword("тест_слово")
storage.add_warn(-100, 7)
storage.set_flag("NIGHT_MODE", True)
storage.add_audit({"ts": "t", "actor": "a", "action": "ban", "target_id": 1})
import importlib
importlib.reload(storage)
storage.load()
check("storage: стоп-слово", "тест_слово" in storage.stopwords())
check("storage: варн", storage.get_warns(-100, 7) == 1)
check("storage: флаг", storage.get_flag("NIGHT_MODE", False) is True)
check("storage: аудит", len(storage.get_audit(5)) >= 1)

# ---- менеджер: изоляция владельцев ----
manager.spawn = lambda e: None
T = "111111:" + "A" * 35
T2 = "222222:" + "B" * 35
T3 = "333333:" + "C" * 35
for fn in (manager.CHILDREN_FILE,):
    try:
        os.remove(fn)
    except OSError:
        pass
manager.add(T, "a", owner=10, password="pa")
manager.add(T2, "b", owner=20, password="pb")
manager.add(T3, "c", owner=10, password="pc")
check("manager: владелец 10 видит 2 бота", len(manager.children(10)) == 2)
check("manager: владелец 20 видит 1 бота", len(manager.children(20)) == 1)
check("manager: чужой не удаляется", manager.remove("222222", owner=10) is False)
check("manager: свой удаляется", manager.remove("222222", owner=20) is True)
check("manager: токен валиден", manager.valid_token(T))
check("manager: мусор не токен", not manager.valid_token("just text"))

# ---- mod-кнопки: срок и banwipe в callback ----
rows = bot.mod_rows(-100, 9)
cbs = [b.callback_data for r in rows for b in r]
check("mod: мут на 3д", "mod:mute:-100:9:259200" in cbs)
check("mod: бан+чистка", "mod:banwipe:-100:9" in cbs)

# ---- панель: все тумблеры разложены по разделам, клавиатуры строятся ----
covered = {k for keys in bot.CAT_FLAGS.values() for k in keys}
allflags = {k for k, _ in bot.PANEL_FLAGS}
check("панель: все флаги в разделах", covered == allflags)
bot.panel_keyboard()
for c in bot.CAT_FLAGS:
    bot.category_keyboard(c)
bot.backup_keyboard()
bot.nums_keyboard()
bot.acts_keyboard()
check("панель: клавиатуры строятся", True)

# ---- кастомные правила со ссылкой ----
r = bot.render_rules("Привет [канал](https://t.me/x) и <b>текст</b>")
check("rules: ссылка кликабельна", '<a href="https://t.me/x">канал</a>' in r)
check("rules: html экранируется", "&lt;b&gt;" in r)
check("rules: t.me без схемы -> https", 'href="https://t.me/y"'
      in bot.render_rules("[c](t.me/y)"))
check("rules: javascript не линкуется", "<a" not in bot.render_rules("[x](javascript:alert(1))"))

# ---- gore-детектор: опционален и мягко деградирует ----
import gore as _gore
check("gore: без load недоступен", _gore.available() is False)
check("gore: detect без модели -> None", _gore.detect(b"x", 0.6) is None)
check("gore: bot подключил модуль", hasattr(bot, "gore"))
check("gore: status() строка", isinstance(_gore.status(), str))
check("gore: size-guard есть", _gore.MAX_BYTES > 0)

# ---- триггеры / автоответы ----
storage.add_trigger("Привет", "Здравствуй, [правила](https://t.me/x)")
check("trigger: сохранён (lower)", "привет" in storage.triggers())
check("trigger: del", storage.del_trigger("привет") and "привет" not in storage.triggers())

# ---- новые команды зарегистрированы ----
cmd_names = {getattr(h.callback, "__name__", "") for h in bot.dp.message.handlers}
for c in ("cmd_diag", "cmd_check", "cmd_addtrigger", "cmd_setwelcome", "on_trigger",
          "cmd_lockdown", "cmd_checkdc", "cmd_purgedc"):
    check(f"handler {c}", c in cmd_names)

# ---- DC-декодер (round-trip) ----
import base64 as _b64m, struct as _st
import dcguard as _dc


def _rle_enc(data):
    out = bytearray(); i = 0
    while i < len(data):
        if data[i] == 0:
            j = i
            while j < len(data) and data[j] == 0 and (j - i) < 255:
                j += 1
            out += bytes([0, j - i]); i = j
        else:
            out.append(data[i]); i += 1
    return bytes(out)


def _fake_fid(dc):
    payload = _st.pack("<II", 2, dc) + b"\x11\x22\x33\x44"
    return _b64m.urlsafe_b64encode(_rle_enc(payload)).decode().rstrip("=")


check("dc: round-trip DC1..5", all(_dc.dc_from_file_id(_fake_fid(d)) == d for d in (1, 2, 3, 4, 5)))
check("dc: мусор -> None", _dc.dc_from_file_id("zzz!!!") is None)


# ---- детект потери прав (по тексту ошибки) ----
async def run_rights():
    alerted = []
    orig = bot.notify_panel

    async def fake(t):
        alerted.append(t)
    bot.notify_panel = fake
    bot.rights_alert.clear()
    await bot._maybe_rights_alert(-100, Exception("Bad Request: not enough rights"))
    await bot._maybe_rights_alert(-100, Exception("just a normal error"))
    bot.notify_panel = orig
    check("rights: алерт на потерю прав, не на прочее", len(alerted) == 1)


asyncio.run(run_rights())


# ---- антирейд автоприёма: всплеск заявок -> AUTO_ACCEPT выкл ----
async def run_burst():
    async def no_admin(c, u):
        return False
    bot.is_admin = no_admin

    async def user_dc(u):
        return None  # DC не мешает
    bot.user_dc = user_dc

    async def approve(c, u):
        return True

    async def noop(*a, **k):
        return None
    bot.bot.approve_chat_join_request = approve
    bot.bot.decline_chat_join_request = noop
    bot.report = noop
    bot.notify_panel = noop
    bot.storage.set_flag("AUTO_ACCEPT", True)
    bot.storage.set_flag("CHECK_JOIN_NAMES", False)
    bot.accept_burst.clear()
    bot.config.ACCEPT_BURST_LIMIT = 10
    bot.config.ACCEPT_BURST_WINDOW = 15
    C = types.SimpleNamespace
    for i in range(11):
        req = C(chat=C(id=-500), from_user=C(id=1000 + i, full_name="U", username=None))
        await bot.on_join_request(req)
    check("burst: автоприём авто-выключился", bot.storage.get_flag("AUTO_ACCEPT", True) is False)


asyncio.run(run_burst())


# ---- /vb голосование + админ-вето, /clearrequests ----
async def run_vote_clear():
    muted = []

    async def mute(c, u, s=None):
        muted.append(u)

    async def yes_admin(c, u):
        return u == 999  # 999 = админ, остальные — нет
    bot.mute_user = mute
    bot.is_admin = yes_admin
    bot.config.VOTE_LIMIT = 3
    bot.config.VOTE_ACTION = "mute"

    class Msg:
        html_text = ""
        async def edit_text(self, *a, **k):
            pass

    def mk_cb(data, uid):
        return types.SimpleNamespace(data=data, from_user=types.SimpleNamespace(id=uid, full_name="V"),
                                     message=Msg(), answer=lambda *a, **k: _noop())

    async def _noop():
        return None

    # голосование за мут юзера 7, порог 3
    bot.votes.clear()
    bot.votes[500] = {"chat": -1, "target": 7, "tname": "T", "starter_name": "S",
                      "yes": {}, "no": set(), "done": False, "ts": bot.now()}
    for voter in (11, 12, 13):
        await bot.on_vote(mk_cb("vote:yes:500", voter))
    check("vote: порог 3 -> мут", muted.count(7) == 1)

    # админ-вето (👎) отменяет
    bot.votes[501] = {"chat": -1, "target": 8, "tname": "T", "starter_name": "S",
                      "yes": {}, "no": set(), "done": False, "ts": bot.now()}
    await bot.on_vote(mk_cb("vote:no:501", 999))  # админ жмёт против
    check("vote: админский 👎 отменяет", 501 not in bot.votes)

    # clear_requests отклоняет все виденные заявки
    declined = []

    async def decl(c, u):
        declined.append(u)
    bot.bot.decline_chat_join_request = decl
    bot.pending_requests[-1] = {101, 102, 103}
    n = await bot.clear_requests(-1, 999)
    check("clearrequests: отклонил все", n == 3 and len(declined) == 3)


asyncio.run(run_vote_clear())


# ---- внутренние роли / права ----
storage.set_role(555, "модератор")
check("role: модератор -> mute есть", bot.has_perm(555, "mute"))
check("role: модератор -> ban нет", not bot.has_perm(555, "ban"))
storage.set_role(556, "старший")
check("role: старший -> ban есть", bot.has_perm(556, "ban"))
storage.set_role(555, None)
check("role: снятие", not bot.has_perm(555, "mute"))

# ---- причина/срок из хвоста команды ----
for toks, exp in [(["3", "дня", "спам"], (259200, "спам")), (["флуд"], (None, "флуд")),
                  (["3ч"], (10800, "")), (["3ч", "флуд"], (10800, "флуд")),
                  (["спам", "тут"], (None, "спам тут")), ([], (None, ""))]:
    check(f"dur+reason {toks}", bot._split_dur_reason(toks) == exp)


# ---- асинхронные: голосование по жалобам ----
async def run_async():
    async def no_admin(c, u):
        return False
    bot.is_admin = no_admin

    async def send(c, t, reply_markup=None, **k):
        return types.SimpleNamespace(message_id=1)

    async def noop(*a, **k):
        return None
    bot.bot.send_message = send
    bot.bot.delete_message = noop
    bot.bot.forward_message = noop
    bot.panel_auth.clear()
    auto = []

    async def mute2(c, u, s=None):
        auto.append("mute")
    bot.mute_user = mute2
    bot.config.REPORT_VOTES = 3
    bot.config.REPORT_COOLDOWN = 0
    bot.config.REPORT_MAX_PER_HOUR = 100
    CH = types.SimpleNamespace(id=-1001234567890, type="supergroup", username="grp")

    def rep(reporter_id):
        r = types.SimpleNamespace(message_id=77, chat=CH,
            from_user=types.SimpleNamespace(id=9, full_name="Spam", username="s"),
            text="spam", caption=None, date=None)
        return types.SimpleNamespace(chat=CH,
            from_user=types.SimpleNamespace(id=reporter_id, full_name="R", username=None),
            reply_to_message=r, text="/report", delete=noop, answer=send)

    bot.report_votes.clear()
    bot.report_cooldown.clear()
    bot.report_times.clear()
    for rid in (101, 102, 103):
        await bot.cmd_report(rep(rid))
    check("report: 3 голоса -> 1 авто-мут", auto.count("mute") == 1)
    bot.report_cooldown.clear()
    await bot.cmd_report(rep(101))  # повтор
    check("report: повтор не считается", len(bot.report_votes[(-1001234567890, 77)]["voters"]) == 3)


asyncio.run(run_async())

print(f"\nИтог: {PASS} ок, {FAIL} провалов.")
sys.exit(1 if FAIL else 0)
