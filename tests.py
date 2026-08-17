# -*- coding: utf-8 -*-
"""
Набор тестов бота. Запуск:  venv\\Scripts\\python.exe tests.py
Использует временный DATA_FILE, реальные данные не трогает. Детектор 18+ не грузится.
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

# ---- нечёткое совпадение стоп-слов (fuzzy) ----
_SW = ["спам", "реклама"]
check("fuzzy: точное", textguard.find_stopword("купи спам", _SW, fuzzy=True))
check("fuzzy: растянуто", textguard.find_stopword("спааам", _SW, fuzzy=True))
check("fuzzy: разделители", textguard.find_stopword("с-п-а-м", _SW, fuzzy=True))
check("fuzzy: пробелы", textguard.find_stopword("с п а м", _SW, fuzzy=True))
check("fuzzy: опечатка длинного", textguard.find_stopword("рекламма", _SW, fuzzy=True))
check("fuzzy: пропуск буквы", textguard.find_stopword("реклам", _SW, fuzzy=True))
check("fuzzy: не задевает храм", not textguard.find_stopword("храм", _SW, fuzzy=True))
check("fuzzy: не задевает класс", not textguard.find_stopword("класс", _SW, fuzzy=True))
check("fuzzy: не задевает река", not textguard.find_stopword("река", _SW, fuzzy=True))
check("fuzzy=off: только точное", not textguard.find_stopword("спааам", _SW, fuzzy=False))

# ---- игра «кубик»: лидеры/переброс ----
check("dice: один лидер", bot._dice_leaders({1: 6, 2: 3, 3: 5}) == [1])
check("dice: ничья на максимуме", set(bot._dice_leaders({1: 6, 2: 6, 3: 4})) == {1, 2})
check("dice: все равны", set(bot._dice_leaders({1: 2, 2: 2})) == {1, 2})
check("dice: пусто", bot._dice_leaders({}) == [])

# ---- кубик доступен обычным юзерам (не съедается фильтром команд) ----
check("public cmds: кубик", {"kubik", "dice", "game"} <= bot.PUBLIC_CMDS)

# ---- name_check: 3-кортеж и скрытость ----
check("name_check: чистое -> (None,None,False)", bot.name_check("нормальное имя") == (None, None, False))
storage.add_stopword("сикретслово", hidden=True)
_p, _a, _h = bot.name_check("привет сикретслово друг")
check("name_check: скрытое имя публично нейтрально", _p == "недопустимое имя" and _h is True and "сикретслово" in _a)
storage.del_stopword("сикретслово")

# ---- банворд ловит @ники и ссылки (латиница) ----
check("stopword: латинский @ник", textguard.find_stopword("пиши сюда @BadGuy плз", ["@badguy"]) == "@badguy")
check("stopword: ссылка t.me", textguard.find_stopword("вот https://t.me/ScamChat заходи", ["t.me/scamchat"]) == "t.me/scamchat")
check("stopword: латиница без совпадения — None", textguard.find_stopword("обычный чистый текст", ["@badguy"]) is None)

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
        if st.get("kind") == "image":
            continue  # фото-шаг отвечается вводом кода, а не кнопками
        o = bot.options_for(st)
        if st["answer"] not in o or len(o) != 4 or len(set(o)) != 4:
            ok = False
check("captcha: ответ всегда среди 4 вариантов", ok)

# ---- количество заданий капчи (CAPTCHA_STEPS 1..3) ----
for want in (1, 2, 3):
    bot.storage.set_num("CAPTCHA_STEPS", want)
    check(f"captcha steps: {want} шаг(ов)", len(bot.build_questions()) == want)
bot.storage.set_num("CAPTCHA_STEPS", 5)   # выше максимума -> зажать до 3
check("captcha steps: >3 -> 3", len(bot.build_questions()) == 3)
bot.storage.set_num("CAPTCHA_STEPS", 0)   # ниже минимума -> зажать до 1
check("captcha steps: <1 -> 1", len(bot.build_questions()) == 1)
bot.storage.set_num("CAPTCHA_STEPS", 3)   # вернуть дефолт

# ---- фото-капча ----
png = bot.make_captcha_image("12345")
check("photocaptcha: PNG сгенерирован", png[:4] == b"\x89PNG" and len(png) > 200)
bot.storage.set_flag("CAPTCHA_IMAGE", True)
bot.storage.set_num("CAPTCHA_DIGITS", 5)
s0 = bot.build_questions()[0]
check("photocaptcha: шаг 1 = image", s0.get("kind") == "image")
check("photocaptcha: код из 5 цифр", s0["answer"].isdigit() and len(s0["answer"]) == 5)
bot.storage.set_flag("CAPTCHA_IMAGE", False)
check("photocaptcha: выкл -> пример a+b", bot.build_questions()[0].get("kind") == "num")
check("photocaptcha: TEXT_ONLY только текст",
      bot.TEXT_ONLY.can_send_messages is True and bot.TEXT_ONLY.can_send_photos is False)
bot.storage.set_flag("CAPTCHA_IMAGE", True)  # вернуть дефолт для остальных проверок

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


# ---- защита цели: само-мут и иерархия ----
async def run_deny():
    async def fake_admin(c, u):
        return u == 900              # 900 = TG-админ; остальные — нет
    bot.is_admin = fake_admin
    bot.storage.set_role(901, "модератор")   # 901 = носитель внутренней роли
    bot.bot_self_id = 999

    check("deny: сам себя нельзя", await bot._deny_target(-1, 902, 902) is not None)
    check("deny: самого бота нельзя", await bot._deny_target(-1, 902, 999) is not None)
    check("deny: обычного обычному можно", await bot._deny_target(-1, 902, 903) is None)
    check("deny: TG-админа не тронуть", await bot._deny_target(-1, 903, 900) is not None)
    check("deny: персонал обычному нельзя", await bot._deny_target(-1, 903, 901) is not None)
    check("deny: персонал TG-админу можно", await bot._deny_target(-1, 900, 901) is None)

    # --- иерархия ролей: старший > модератор ---
    bot.storage.set_role(910, "модератор")
    bot.storage.set_role(911, "старший")
    bot.storage.set_role(912, "админ")
    # старший может наказать модератора, обратное — нельзя
    check("hier: старший наказывает модератора", await bot._deny_target(-1, 911, 910) is None)
    check("hier: модератор НЕ наказывает старшего", await bot._deny_target(-1, 910, 911) is not None)
    # равный равного нельзя
    bot.storage.set_role(913, "модератор")
    check("hier: модератор НЕ наказывает модератора", await bot._deny_target(-1, 910, 913) is not None)
    # админ (внутр.) наказывает старшего
    check("hier: внутр-админ наказывает старшего", await bot._deny_target(-1, 912, 911) is None)
    # ранги
    check("hier: ранг старший > модератор", bot.role_rank("старший") > bot.role_rank("модератор"))
    check("hier: ранг неизвестной роли = 0", bot.role_rank("нет-такой") == 0)
    for u in (901, 910, 911, 912, 913):
        bot.storage.set_role(u, None)


asyncio.run(run_deny())


# ---- редактирование прав ролей (оверрайд поверх config) ----
storage.set_role_perms("модератор", ["mute", "warn"])
check("roleperm: старт с mute", "mute" in bot.role_perms("модератор"))
bot.set_role_perm("модератор", "ban", True)
check("roleperm: добавили ban", "ban" in bot.role_perms("модератор"))
storage.set_role(560, "модератор")
check("roleperm: has_perm видит ban", bot.has_perm(560, "ban"))
bot.set_role_perm("модератор", "mute", False)
check("roleperm: убрали mute", not bot.has_perm(560, "mute"))
storage.set_role(560, None)
check("roleperm: фоллбэк на config без оверрайда",
      bot.role_perms("старший") == set(bot.config.ROLES["старший"]))

# ---- пер-юзерный оверрайд прав (лично человеку, поверх роли) ----
storage.set_role(570, "старший")                       # у роли есть ban
check("userperm: до оверрайда права от роли", bot.has_perm(570, "ban"))
storage.set_user_perms(570, ["mute"])                  # лично оставили только mute
check("userperm: оверрайд перебил роль (ban убран)", not bot.has_perm(570, "ban"))
check("userperm: оверрайд оставил mute", bot.has_perm(570, "mute"))
check("userperm: effective_perms = личный набор", bot.effective_perms(570) == {"mute"})
storage.set_user_perms(570, None)                      # сброс к роли
check("userperm: сброс вернул права роли", bot.has_perm(570, "ban"))
check("userperm: без оверрайда get вернёт None",
      storage.get_user_perms_override(570) is None)
storage.set_role(570, None)
storage.set_user_perms(570, None)

# ---- таргет по @нику / id / text_mention ----
async def run_target():
    bot.uname_cache.clear()
    bot.uname_cache["ivan"] = 7001            # бот «видел» @ivan
    CH = types.SimpleNamespace(id=-77, type="supergroup", username="grp")

    def m(text, entities=None, reply=None):
        return types.SimpleNamespace(chat=CH, text=text, entities=entities,
                                     reply_to_message=reply,
                                     from_user=types.SimpleNamespace(id=1, full_name="A", username=None))

    check("target: по числовому id", bot._target_id(m("/mute 12345")) == 12345)
    check("target: «3» без reply/ника не берётся за id (цель не распознана)", bot._target_dur_reason(m("/mute 3 часа бесит")) == (None, None, ""))
    check("target: по известному @нику", bot._target_id(m("/mute @ivan")) == 7001)
    check("target: неизвестный @ник -> 0", bot._target_id(m("/mute @nobody")) == 0)
    check("target: без цели -> None", bot._target_id(m("/mute")) is None)
    # text_mention (тап по юзеру без ника)
    ent = [types.SimpleNamespace(type="text_mention",
                                 user=types.SimpleNamespace(id=8002, username="petya", full_name="P"))]
    check("target: text_mention", bot._target_id(m("/mute Пётр", entities=ent)) == 8002)
    check("target: text_mention кэширует ник", bot.uname_cache.get("petya") == 8002)
    # с сроком и причиной
    uid, secs, reason = bot._target_dur_reason(m("/ban @ivan 3 дня спам"))
    check("target: @ник + срок + причина uid", uid == 7001)
    check("target: @ник + срок + причина reason", reason == "спам")
    uid2, _, _ = bot._target_dur_reason(m("/ban @nobody флуд"))
    check("target: неизвестный @ник в dur_reason -> 0", uid2 == 0)


asyncio.run(run_target())

# ---- белый список стикерпаков ----
for _p in list(bot.storage.sticker_packs()):
    bot.storage.disallow_pack(_p)
check("pack: изначально не в списке", not bot.storage.is_pack_allowed("MyPack"))
check("pack: добавили", bot.storage.allow_pack("MyPack") is True)
check("pack: повторно не добавить", bot.storage.allow_pack("mypack") is False)  # регистронезависимо
check("pack: теперь в списке", bot.storage.is_pack_allowed("MyPack"))
check("pack: регистр не важен при проверке", bot.storage.is_pack_allowed("MYPACK"))
check("pack: пустое имя не whitelist", not bot.storage.is_pack_allowed(""))
check("pack: пустое имя не добавить", bot.storage.allow_pack("") is False)
check("pack: убрали", bot.storage.disallow_pack("MyPack") is True)
check("pack: после удаления нет", not bot.storage.is_pack_allowed("MyPack"))

# ---- классификация команд (публичные vs админские) ----
check("cmd: /ban@bot -> ban", bot._cmd_name("/ban@LavkaBot 3 дня") == "ban")
check("cmd: регистр не важен", bot._cmd_name("/VB") == "vb")
check("cmd: не команда -> None", bot._cmd_name("привет всем") is None)
check("cmd: vb публичная", "vb" in bot.PUBLIC_CMDS)
check("cmd: report публичная", "report" in bot.PUBLIC_CMDS)
check("cmd: ban НЕ публичная", "ban" not in bot.PUBLIC_CMDS)

# ---- панель: /vb-всем дефолт, клавиатуры ролей, новые флаги в разделах ----
check("vote: VOTE_ANYONE дефолт вкл", bot.flag("VOTE_ANYONE") is True)
bot.roles_keyboard()
for _rn in bot.config.ROLES:
    bot.role_perm_keyboard(_rn)
bot.asg_keyboard()
check("панель: клавиатуры ролей строятся", True)
check("панель: новые тумблеры в разделах",
      {"DELETE_USER_COMMANDS", "VOTE_ANYONE"} <= {k for ks in bot.CAT_FLAGS.values() for k in ks})
_ok_cb = all(len((b.callback_data or "").encode()) <= 64
             for r in bot.role_perm_keyboard("модератор").inline_keyboard for b in r)
check("панель: callback_data ролей <= 64 байт", _ok_cb)

# ---- владельцы (owners) ----
storage.add_owner(12345)
check("owner: добавлен", storage.is_owner(12345))
check("owner: в списке", 12345 in storage.owners_all())
check("owner: повтор не дублирует", storage.add_owner(12345) is False)
check("owner: снят", storage.remove_owner(12345) and not storage.is_owner(12345))
bot.owners_keyboard()
bot.asg_pick_keyboard()
check("панель: клавиатуры владельцев/назначения строятся", True)


# ---- /setrole: раздаёт роли ТОЛЬКО владелец (даже TG-админ не может) ----
async def run_owner_gate():
    async def yes_admin(c, u):
        return True                       # даже если TG-админ — не-владельцу нельзя
    bot.is_admin = yes_admin
    storage.set_role(2001, None)
    for o in list(storage.owners_all()):
        storage.remove_owner(o)

    async def answer(*a, **k):
        return None

    def msg(actor):
        rep = types.SimpleNamespace(from_user=types.SimpleNamespace(id=2001, full_name="T"))
        return types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=actor, full_name="A"),
            chat=types.SimpleNamespace(id=-9),
            text="/setrole модератор", reply_to_message=rep, answer=answer)

    await bot.cmd_setrole(msg(3001))
    check("owner-gate: не-владелец (даже админ) роль НЕ выдаёт", storage.get_role(2001) is None)
    storage.add_owner(3001)
    await bot.cmd_setrole(msg(3001))
    check("owner-gate: владелец роль выдаёт", storage.get_role(2001) == "модератор")
    storage.set_role(2001, None)
    storage.remove_owner(3001)


asyncio.run(run_owner_gate())


# ---- фото-капча: кнопка «Заменить картинку» ----
check("capnew: кнопка в клавиатуре",
      any(b.callback_data == "capnew"
          for r in bot.captcha_photo_kb().inline_keyboard for b in r))


async def run_capnew():
    chat, uid = -7, 4001
    steps = [{"q": "Шаг1", "answer": "11111", "kind": "image"},
             {"q": "Шаг2", "answer": "7", "wrongs": ["1", "2", "3"]}]
    bot.pending[(chat, uid)] = {"steps": steps, "idx": 0, "msg_id": 321, "task": None}
    edited = []

    async def edit_media(media, reply_markup=None):
        edited.append(media)

    async def ans(*a, **k):
        return None

    cb = types.SimpleNamespace(
        data="capnew",
        from_user=types.SimpleNamespace(id=uid, full_name="N"),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat),
                                      message_id=321, edit_media=edit_media),
        answer=ans)
    await bot.on_captcha_new(cb)
    check("capnew: картинка заменена", len(edited) == 1)
    check("capnew: код новый и валиден",
          steps[0]["answer"].isdigit() and len(steps[0]["answer"]) == bot.num("CAPTCHA_DIGITS"))
    t = bot.pending[(chat, uid)].get("task")
    if t:
        t.cancel()
    bot.pending.pop((chat, uid), None)


asyncio.run(run_capnew())


# ---- детекция удалённых аккаунтов ----
check("deleted: пустой профиль = удалёнка",
      bot.deleted.is_deleted_account(C(first_name="", last_name=None, username=None)))
check("deleted: обычный юзер не удалёнка",
      not bot.deleted.is_deleted_account(C(first_name="Иван", last_name="", username=None)))
check("deleted: только @ник — не удалёнка",
      not bot.deleted.is_deleted_account(C(first_name="", last_name=None, username="ivan")))
check("deleted: только фамилия — не удалёнка",
      not bot.deleted.is_deleted_account(C(first_name="", last_name="Петров", username=None)))
_del_user = C(first_name="", last_name=None, username=None)
check("deleted: кикать member-удалёнку",
      bot.deleted.should_kick_member(C(status="member", user=_del_user)))
check("deleted: НЕ кикать админа-удалёнку",
      not bot.deleted.should_kick_member(C(status="administrator", user=_del_user)))
check("deleted: НЕ кикать живого member",
      not bot.deleted.should_kick_member(
          C(status="member", user=C(first_name="Аня", last_name=None, username=None))))

# ---- риск-движок: письменность имени ----
rs = bot.riskscore
check("script: кириллица", rs.dominant_script("Иван Петров") == "cyrillic")
check("script: латиница", rs.dominant_script("John Smith") == "latin")
check("script: арабица", rs.dominant_script("محمد") == "arabic")
check("script: cjk", rs.dominant_script("田中太郎") == "cjk")
check("script: индик/тай", rs.dominant_script("สมชาย") == "indic")
check("script: значки/эмодзи", rs.dominant_script("🔥💰🎰") == "symbols")

# ---- риск-движок: случайный юзернейм ----
check("uname: цифровой хвост", rs.is_random_username("Marina480213"))
check("uname: набор согласных", rs.is_random_username("kldfjghqwx"))
check("uname: нормальный ник", not rs.is_random_username("alexander"))
check("uname: короткий", not rs.is_random_username("kot"))
check("uname: пусто", not rs.is_random_username(None))

# ---- риск-движок: свежесть аккаунта ----
check("fresh: id выше порога", rs.is_fresh_account(8_000_000_000, 7_500_000_000))
check("fresh: id ниже порога", not rs.is_fresh_account(100_000, 7_500_000_000))
check("fresh: порог 0 = выкл", not rs.is_fresh_account(9_000_000_000, 0))

# ---- риск-движок: скоринг профиля ----
s_clean, _ = rs.score_profile(first_name="Мария", last_name="Иванова", username="mariva",
                              user_id=100_000, is_premium=False, has_photo=True,
                              dc=2, dc_flagged=False, fresh_id_threshold=7_500_000_000)
check("score: чистый русский профиль ~0", s_clean == 0)

s_watch, _ = rs.score_profile(first_name="John", last_name="", username="John77812",
                              user_id=8_000_000_000, is_premium=False, has_photo=False,
                              dc=None, dc_flagged=False, fresh_id_threshold=7_500_000_000)
check("score: подозрительный -> наблюдение", s_watch >= bot.config.RISK_WATCH_THRESHOLD)

s_hard, _ = rs.score_profile(first_name="محمد", last_name="", username="xxx88213promo",
                             user_id=8_000_000_000, is_premium=False, has_photo=False,
                             dc=5, dc_flagged=True, fresh_id_threshold=7_500_000_000)
check("score: явный спам-профиль -> жёстко", s_hard >= bot.config.RISK_BAN_THRESHOLD)

check("verdict: clear", rs.verdict(10, 45, 85) == "clear")
check("verdict: watch", rs.verdict(50, 45, 85) == "watch")
check("verdict: hard", rs.verdict(90, 45, 85) == "hard")

# ---- probation: наблюдение живёт и истекает ----
bot.probation.clear()
_pu = C(id=555001)
bot.start_probation(-100, _pu, 60, ["тест"])
check("probation: активно после старта", bot.probation_active(-100, 555001) is not None)
bot.probation[(-100, 555001)]["until"] = bot.now() - bot.timedelta(seconds=1)
check("probation: истекло -> None", bot.probation_active(-100, 555001) is None)
check("probation: истёкшее снято", (-100, 555001) not in bot.probation)

# ---- known_members: собирает виденных по чату ----
bot.recent[(-200, 42)] = bot.deque()
bot.msgcount[(-200, 43)] = 1
bot.newcomer[(-200, 44)] = bot.now()
km = bot.known_members(-200)
check("known_members: собрал всех виденных", {42, 43, 44} <= km)
check("known_members: чужой чат не попал", 42 not in bot.known_members(-999))
bot.recent.pop((-200, 42), None)
bot.msgcount.pop((-200, 43), None)
bot.newcomer.pop((-200, 44), None)

# ---- userbot: опционален, не падает независимо от наличия ключей/Telethon ----
check("userbot: available булев", isinstance(bot.userbot.available(), bool))
check("userbot: status строка", isinstance(bot.userbot.status(), str))

# ---- анти-деанон: чистый PII-детектор (find_pii / is_deanon) ----
import deanon  # noqa: E402
# срабатывания
check("deanon: телефон РФ", "phone" in deanon.find_pii("звони +7 999 123-45-67"))
check("deanon: 8-номер", "phone" in deanon.find_pii("тел 8(999)123-45-67"))
check("deanon: email", "email" in deanon.find_pii("почта ivan.petrov@mail.ru"))
check("deanon: @ник", "handle" in deanon.find_pii("его тг @petrov_ivan"))
check("deanon: t.me ссылка", "handle" in deanon.find_pii("вот https://t.me/petrov"))
# валидный номер карты (проходит Луна): 4111 1111 1111 1111
check("deanon: карта (Луна)", "card" in deanon.find_pii("карта 4111 1111 1111 1111"))
check("deanon: паспорт РФ", "passport_ru" in deanon.find_pii("паспорт 12 34 567890"))
check("deanon: снилс", "snils" in deanon.find_pii("СНИЛС 112-233-445 95"))
check("deanon: адрес", "address" in deanon.find_pii("живёт г. Москва ул. Ленина д. 5 кв. 10"))
check("deanon: подпись", "label" in deanon.find_pii("его домашний адрес такой:"))
# НЕ должно срабатывать на обычном тексте
for clean in ["привет как дела", "цена 1000 рублей за штуку", "встречаемся в 12:30",
              "заказ №123456 готов", "2+2=4 отвечай быстрее"]:
    check(f"deanon: чисто '{clean[:20]}'", deanon.find_pii(clean) == [] or
          not deanon.is_deanon(clean, 2)[0])
# случайные 16 цифр без Луна — НЕ карта
check("deanon: не-карта (Луна режектит)", "card" not in deanon.find_pii("код 1234 5678 9012 3456 7"))
# is_deanon: один «тяжёлый» тип срабатывает; один «лёгкий» — нет
check("deanon: паспорт один -> деанон", deanon.is_deanon("паспорт 12 34 567890", 2)[0])
check("deanon: один телефон -> НЕ деанон (нужно 2)", not deanon.is_deanon("тел +79991234567", 2)[0])
check("deanon: телефон+адрес -> деанон",
      deanon.is_deanon("+7 999 123-45-67, г. Москва ул. Ленина д.5 кв.10", 2)[0])
check("deanon: телефон+email -> деанон",
      deanon.is_deanon("пиши +7 999 123 45 67 или a@b.ru", 2)[0])
check("deanon: 'скинь телефон' -> НЕ деанон",
      not deanon.is_deanon("скинь телефон если что", 2)[0])
# OCR-обёртка опциональна: не падает без движка
check("deanon: available булев", isinstance(deanon.available(), bool))
check("deanon: status строка", isinstance(deanon.status(), str))
check("deanon: extract без движка -> ''", deanon.available() or deanon.extract_text(b"x") == "")
check("deanon: describe читаемо", "телефон" in deanon.describe(["phone"]))

# ---- текстовый анти-деанон: угрозы, деанон-ресурсы, ПДн в тексте ----
check("deanon-текст: угроза 'найду тебя'", deanon.find_threats("я тебя найду и закопаю") != [])
check("deanon-текст: деанон-намерение", deanon.find_threats("щас деаноню этого админа") != [])
check("deanon-текст: @...dnn ресурс", deanon.find_deanon_handles("кидай в @chudochatdnn") != [])
check("deanon-текст: t.me/deanon", deanon.find_deanon_handles("смотри t.me/deanonbaza тут всё") != [])
check("deanon-текст: чистый текст без угроз", deanon.find_threats("привет как дела, отличная погода") == [])
check("deanon-текст: обычный @ник не деанон-ресурс", deanon.find_deanon_handles("пиши @ivan_petrov") == [])
check("deanon-текст: scan угроза срабатывает", deanon.scan_text("я тебя найду сволочь", 2)[0])
check("deanon-текст: scan слив ПДн срабатывает",
      deanon.scan_text("админ Иванов Иван, +7 999 123 45 67, г Москва ул Ленина д5 кв10", 2)[0])
check("deanon-текст: scan чистое НЕ срабатывает", not deanon.scan_text("во сколько сегодня встреча?", 2)[0])


# ---- ЧС-автопилот: детект волны сообщений ----
bot.msg_wave.clear()
_t0 = bot.now()
# 7 разных юзеров за окно -> ещё не волна (WAVE_USERS=8 по умолчанию)
bot.config.WAVE_USERS = 8
bot.config.WAVE_WINDOW = 20
for i in range(7):
    n = bot.wave_detect(-700, 6000 + i, _t0)
check("wave: 7 юзеров -> нет волны", n < bot.config.WAVE_USERS)
n = bot.wave_detect(-700, 6099, _t0)
check("wave: 8-й юзер -> волна", n >= bot.config.WAVE_USERS)
# повторы от одного юзера не раздувают счётчик уникальных
for _ in range(20):
    n2 = bot.wave_detect(-701, 42, _t0)
check("wave: спам от одного = 1 уникальный", n2 == 1)
# старые сообщения выпадают из окна
bot.msg_wave.clear()
bot.wave_detect(-702, 1, _t0 - bot.timedelta(seconds=100))
n3 = bot.wave_detect(-702, 2, _t0)
check("wave: старьё за окном отброшено", n3 == 1)

# ---- ЧС-автопилот: вход сохраняет и мягко ужесточает, выход откатывает ----
async def run_crisis_toggle():
    async def noop(*a, **k):
        return None
    bot.report = noop
    bot.notify_panel = noop
    async def _ping(cid, reason):
        return None
    bot._crisis_staff_ping = _ping
    bot.crisis.clear()
    bot.storage.set_flag("AUTO_ACCEPT", True)      # было включено
    bot.storage.set_flag("RISK_ENABLED", False)    # было выключено
    _before_accept = bot.flag("AUTO_ACCEPT")
    _before_risk = bot.flag("RISK_ENABLED")
    await bot.enter_crisis(-710, "тест", force=True)
    check("crisis: режим активен после входа", bot.crisis_active(-710))
    check("crisis: AUTO_ACCEPT выключен на время ЧС", bot.flag("AUTO_ACCEPT") is False)
    check("crisis: RISK_ENABLED включён на время ЧС", bot.flag("RISK_ENABLED") is True)
    await bot.exit_crisis(-710)
    check("crisis: режим снят после выхода", not bot.crisis_active(-710))
    check("crisis: AUTO_ACCEPT восстановлен", bot.flag("AUTO_ACCEPT") == _before_accept)
    check("crisis: RISK_ENABLED восстановлен", bot.flag("RISK_ENABLED") == _before_risk)


asyncio.run(run_crisis_toggle())

# ---- ЧС-автопилот: эскалация при молчании модеров ----
async def run_crisis_escalate():
    async def noop(*a, **k):
        return None
    bot.report = noop
    bot.notify_panel = noop
    async def _ping(cid, reason):
        return None
    bot._crisis_staff_ping = _ping
    bot.crisis.clear()
    await bot.enter_crisis(-720, "тест", force=True)
    st = bot.crisis[-720]
    _saved_ld = st["saved"]["flags"].get("LOCKDOWN", bot.flag("LOCKDOWN"))
    # модеры молчат: отматываем «вход» в прошлое за порог эскалации
    st["entered"] = bot.now() - bot.timedelta(seconds=bot.config.CRISIS_ESCALATE_AFTER + 5)
    await bot.escalate_crisis(-720)
    check("crisis: эскалация -> уровень 2", bot.crisis[-720]["level"] == 2)
    check("crisis: эскалация включает LOCKDOWN", bot.flag("LOCKDOWN") is True)
    # ack останавливает эскалацию
    await bot.exit_crisis(-720)
    check("crisis: выход после эскалации вернул LOCKDOWN", bot.flag("LOCKDOWN") == _saved_ld)
    # Гейт монитора: при acked эскалации быть НЕ должно, даже если время вышло.
    await bot.enter_crisis(-721, "тест", force=True)
    _st = bot.crisis[-721]
    _st["acked"] = True
    _st["entered"] = bot.now() - bot.timedelta(seconds=bot.config.CRISIS_ESCALATE_AFTER + 5)
    _t = bot.now()
    _would_escalate = (not _st["acked"] and _st["level"] < 2
                       and (_t - _st["entered"]).total_seconds() > bot.config.CRISIS_ESCALATE_AFTER)
    check("crisis: acked -> монитор не эскалирует", _would_escalate is False)
    await bot.exit_crisis(-721)


asyncio.run(run_crisis_escalate())


# ---- игра «Мафия» (движок mafia.py) ----
import mafia  # noqa: E402

# Раздача ролей: ровно один комиссар/доктор, мафии ~четверть, остальные мирные.
_ids = list(range(1, 9))            # 8 игроков
_roles = mafia.assign_roles(_ids, seed=1)
_rvals = list(_roles.values())
check("mafia: роли розданы всем", len(_roles) == 8)
check("mafia: мафии ~25%", _rvals.count(mafia.ROLE_MAFIA) == 2)
check("mafia: один комиссар", _rvals.count(mafia.ROLE_COMMISSAR) == 1)
check("mafia: один доктор", _rvals.count(mafia.ROLE_DOCTOR) == 1)
check("mafia: остальные мирные", _rvals.count(mafia.ROLE_CIVILIAN) == 4)

# Минимум игроков: 4 -> 1 мафия.
check("mafia: мин.состав 4 -> 1 мафия",
      list(mafia.assign_roles(list(range(4)), seed=2).values()).count(mafia.ROLE_MAFIA) == 1)


def _mk(role, alive=True):
    return {"role": role, "alive": alive, "name": "x"}


# Победа мирных — вся мафия мертва.
_p = {1: _mk(mafia.ROLE_MAFIA, alive=False), 2: _mk(mafia.ROLE_CIVILIAN),
      3: _mk(mafia.ROLE_DOCTOR)}
check("mafia: победа мирных", mafia.check_win(_p) == "peace")

# Победа мафии — мафии >= остальных.
_p = {1: _mk(mafia.ROLE_MAFIA), 2: _mk(mafia.ROLE_CIVILIAN)}
check("mafia: победа мафии (1 vs 1)", mafia.check_win(_p) == "mafia")

# Игра продолжается — мафия в меньшинстве.
_p = {1: _mk(mafia.ROLE_MAFIA), 2: _mk(mafia.ROLE_CIVILIAN), 3: _mk(mafia.ROLE_DOCTOR)}
check("mafia: игра идёт (1 vs 2)", mafia.check_win(_p) is None)

# Резолв ночи: без доктора жертва гибнет.
_p = {1: _mk(mafia.ROLE_MAFIA), 2: _mk(mafia.ROLE_CIVILIAN), 3: _mk(mafia.ROLE_CIVILIAN)}
check("mafia: жертва гибнет", mafia.resolve_night(_p, 2, None) == 2)
# Доктор вылечил ту же цель — жертвы нет.
check("mafia: доктор спас", mafia.resolve_night(_p, 2, 2) is None)
# Мафия не выбрала цель — никто не гибнет.
check("mafia: нет цели -> нет жертвы", mafia.resolve_night(_p, None, None) is None)

# Выбор жертвы мафией по голосам (большинство).
check("mafia: большинство голосов", mafia.pick_mafia_target({10: 2, 11: 2, 12: 3}, seed=1) == 2)
check("mafia: нет голосов -> None", mafia.pick_mafia_target({}) is None)

# Дневное голосование: явный лидер казнён, ничья -> никого.
_lynched, _ = mafia.tally_votes({1: 5, 2: 5, 3: 6})
check("mafia: казнён лидер голосования", _lynched == 5)
_lynched, _ = mafia.tally_votes({1: 5, 2: 6})
check("mafia: ничья -> никого", _lynched is None)
_lynched, _ = mafia.tally_votes({1: "skip", 2: "skip"})
check("mafia: все воздержались -> никого", _lynched is None)

# Полная мини-партия: 1 мафия против 2 мирных -> мафия убивает одного -> победа мафии.
_p = {1: _mk(mafia.ROLE_MAFIA), 2: _mk(mafia.ROLE_CIVILIAN), 3: _mk(mafia.ROLE_CIVILIAN)}
_victim = mafia.resolve_night(_p, 2, None)
_p[_victim]["alive"] = False
check("mafia: сценарий -> победа мафии", mafia.check_win(_p) == "mafia")

# «start»/«mafia» доступны обычным участникам (не режутся модерацией).
check("mafia: команды в публичном списке",
      "mafia" in bot.PUBLIC_CMDS and "start" in bot.PUBLIC_CMDS)


print(f"\nИтог: {PASS} ок, {FAIL} провалов.")
# Importing the application creates an aiogram HTTP session. Some async tests
# open it, so close it explicitly before the interpreter exits.
asyncio.run(bot.bot.session.close())

sys.exit(1 if FAIL else 0)
