# -*- coding: utf-8 -*-
"""
Чистая логика Texas Hold'em без зависимостей от aiogram.

Хэндлеры, таймеры и рендер в чат/ЛС живут в bot.py; здесь — только состояние
стола, правила торговли, вскрытие и подсчёт банка.
"""

from __future__ import annotations

import itertools
import random
from collections import Counter

STARTING_STACK = 50_000
SMALL_BLIND = 500
BIG_BLIND = 1_000
TURN_TIMEOUT_SEC = 60
MAX_TIMEOUTS = 2
MIN_PLAYERS = 2
MAX_PLAYERS = 9

RANKS = "23456789TJQKA"
SUITS = "cdhs"
RANK_VALUE = {r: i for i, r in enumerate(RANKS, start=2)}
SUIT_ICON = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}
HAND_NAMES = {
    8: "стрит-флеш",
    7: "каре",
    6: "фулл-хаус",
    5: "флеш",
    4: "стрит",
    3: "сет",
    2: "две пары",
    1: "пара",
    0: "старшая карта",
}


def new_table(host_id: int) -> dict:
    return {
        "phase": "lobby",
        "host": host_id,
        "players": {},
        "seats": [],
        "dealer_index": -1,
        "hand_no": 0,
        "deck": [],
        "board": [],
        "pot": 0,
        "street": None,
        "current_turn": None,
        "current_bet": 0,
        "min_raise": BIG_BLIND,
        "small_blind": SMALL_BLIND,
        "big_blind": BIG_BLIND,
        "last_event": "",
        "turn_token": 0,
    }


def add_player(table: dict, uid: int, name: str) -> bool:
    if table.get("phase") != "lobby":
        return False
    if uid in table["players"]:
        table["players"][uid]["name"] = name
        return True
    if len(table["seats"]) >= MAX_PLAYERS:
        return False
    table["players"][uid] = {
        "name": name,
        "stack": STARTING_STACK,
        "in_table": True,
        "misses": 0,
        "hole": [],
        "folded": False,
        "all_in": False,
        "street_bet": 0,
        "total_bet": 0,
        "acted": False,
        "last_action": "",
        "hand_rank": None,
        "showdown_name": "",
        "disqualified": False,
    }
    table["seats"].append(uid)
    return True


def active_table_players(table: dict) -> list[int]:
    players = table["players"]
    return [u for u in table["seats"] if players[u].get("in_table") and players[u].get("stack", 0) > 0]


def seated_count(table: dict) -> int:
    return len(active_table_players(table))


def format_card(card: str) -> str:
    return f"{card[0]}{SUIT_ICON.get(card[1], card[1])}"


def format_cards(cards: list[str]) -> str:
    return " ".join(format_card(c) for c in cards) if cards else "—"


def start_tournament(table: dict, seed: int | None = None) -> dict:
    if table.get("phase") != "lobby":
        return {"ok": False, "reason": "already_started"}
    if seated_count(table) < MIN_PLAYERS:
        return {"ok": False, "reason": "not_enough_players"}
    table["phase"] = "playing"
    table["hand_no"] = 0
    table["dealer_index"] = -1
    table["last_event"] = "🃏 Турнир Texas Hold'em начался."
    begin_hand(table, seed=seed)
    return {"ok": True}


def _participants(table: dict) -> list[int]:
    players = table["players"]
    return [u for u in table["seats"] if players[u].get("in_table") and players[u].get("stack", 0) > 0]


def _in_hand(table: dict) -> list[int]:
    # «В руке» = сдали карты в этой раздаче и игрок не покинул стол.
    # ВАЖНО: не фильтруем по stack > 0 — иначе all-in игрок (stack == 0)
    # выпадает из раздачи, ломает вскрытие и подвешивает ход.
    players = table["players"]
    return [u for u in table["seats"] if players[u].get("in_table") and players[u].get("hole")]


def _not_folded(table: dict) -> list[int]:
    players = table["players"]
    return [u for u in _in_hand(table) if not players[u].get("folded")]


def _actionable(table: dict) -> list[int]:
    players = table["players"]
    return [u for u in _not_folded(table) if not players[u].get("all_in") and players[u].get("stack", 0) > 0]


def _seat_index(table: dict, uid: int) -> int:
    return table["seats"].index(uid)


def _next_from_index(table: dict, start_index: int, pool: list[int]) -> int | None:
    seats = table["seats"]
    if not pool:
        return None
    for step in range(1, len(seats) + 1):
        idx = (start_index + step) % len(seats)
        uid = seats[idx]
        if uid in pool:
            return uid
    return None


def _move_button(table: dict) -> int | None:
    players = _participants(table)
    if len(players) < 2:
        return None
    start = table.get("dealer_index", -1)
    seats = table["seats"]
    for step in range(1, len(seats) + 1):
        idx = (start + step) % len(seats)
        uid = seats[idx]
        if uid in players:
            table["dealer_index"] = idx
            return uid
    return None


def _post(table: dict, uid: int, amount: int) -> int:
    p = table["players"][uid]
    paid = min(amount, p["stack"])
    p["stack"] -= paid
    p["street_bet"] += paid
    p["total_bet"] += paid
    table["pot"] += paid
    if p["stack"] == 0:
        p["all_in"] = True
    return paid


def begin_hand(table: dict, seed: int | None = None) -> dict:
    players = table["players"]
    alive = _participants(table)
    if len(alive) < 2:
        _finish_tournament(table)
        return {"ok": False, "reason": "finished"}

    table["hand_no"] += 1
    table["phase"] = "playing"
    table["street"] = "preflop"
    table["board"] = []
    table["pot"] = 0
    table["current_bet"] = 0
    table["min_raise"] = BIG_BLIND
    table["current_turn"] = None
    table["deck"] = [r + s for r in RANKS for s in SUITS]
    random.Random(seed).shuffle(table["deck"])

    for uid, p in players.items():
        p["hole"] = []
        p["folded"] = False
        p["all_in"] = False
        p["street_bet"] = 0
        p["total_bet"] = 0
        p["acted"] = False
        p["last_action"] = ""
        p["hand_rank"] = None
        p["showdown_name"] = ""
        if p.get("stack", 0) <= 0:
            p["in_table"] = False

    dealer = _move_button(table)
    dealer_idx = _seat_index(table, dealer)

    if len(alive) == 2:
        sb_uid = dealer
        bb_uid = _next_from_index(table, dealer_idx, alive)
    else:
        sb_uid = _next_from_index(table, dealer_idx, alive)
        bb_uid = _next_from_index(table, _seat_index(table, sb_uid), alive)

    for _ in range(2):
        for uid in alive:
            players[uid]["hole"].append(table["deck"].pop())

    sb_paid = _post(table, sb_uid, table["small_blind"])
    bb_paid = _post(table, bb_uid, table["big_blind"])
    players[sb_uid]["last_action"] = f"SB {sb_paid}"
    players[bb_uid]["last_action"] = f"BB {bb_paid}"
    table["current_bet"] = players[bb_uid]["street_bet"]
    table["current_turn"] = _next_to_act(table, bb_uid)
    table["last_event"] = (
        f"🃏 Раздача #{table['hand_no']}. Блайнды: {players[sb_uid]['name']} SB {sb_paid}, "
        f"{players[bb_uid]['name']} BB {bb_paid}."
    )

    if table["current_turn"] is None:
        _runout_and_showdown(table)
    return {"ok": True}


def player_to_call(table: dict, uid: int) -> int:
    return max(0, table.get("current_bet", 0) - table["players"][uid].get("street_bet", 0))


def allowed_actions(table: dict, uid: int) -> dict:
    if table.get("phase") != "playing" or table.get("current_turn") != uid:
        return {}
    p = table["players"].get(uid)
    if not p or p.get("folded") or p.get("all_in") or not p.get("in_table"):
        return {}

    owe = player_to_call(table, uid)
    stack = p["stack"]
    current_total = p["street_bet"] + stack
    opts = {
        "fold": owe > 0,
        "check": owe == 0,
        "call": owe > 0 and stack > 0,
        "call_amount": min(owe, stack),
        "raise_to": [],
        "all_in": stack > 0,
        "all_in_to": current_total,
    }

    if stack <= owe:
        return opts

    # Игрок уже действовал на этой улице и оказался на ходу снова только из-за
    # НЕПОЛНОГО all-in соперника (докол меньше min_raise). Такой докол торги не
    # переоткрывает — рейзить нельзя, доступны лишь call/fold.
    if p.get("acted"):
        return opts

    min_to = table["current_bet"] + max(table.get("min_raise", BIG_BLIND), BIG_BLIND)
    if table["current_bet"] == 0:
        min_to = max(BIG_BLIND, min_to)

    if current_total > table["current_bet"] and min_to <= current_total:
        opts["raise_to"].append(min_to)
        double_to = min_to + max(table.get("min_raise", BIG_BLIND), BIG_BLIND)
        if double_to <= current_total and double_to != min_to:
            opts["raise_to"].append(double_to)
    return opts


def apply_action(table: dict, uid: int, action: str, amount: int | None = None) -> dict:
    if table.get("phase") != "playing" or table.get("current_turn") != uid:
        return {"ok": False, "reason": "not_your_turn"}

    p = table["players"].get(uid)
    if not p or p.get("folded") or p.get("all_in") or not p.get("in_table"):
        return {"ok": False, "reason": "inactive"}

    owe = player_to_call(table, uid)
    name = p["name"]

    if action == "fold":
        p["folded"] = True
        p["acted"] = True
        p["last_action"] = "fold"
        table["last_event"] = f"↩️ {name} сбросил карты."
    elif action == "check":
        if owe != 0:
            return {"ok": False, "reason": "cannot_check"}
        p["acted"] = True
        p["last_action"] = "check"
        table["last_event"] = f"✅ {name} сказал check."
    elif action == "call":
        if owe <= 0:
            return {"ok": False, "reason": "nothing_to_call"}
        paid = _post(table, uid, owe)
        p["acted"] = True
        p["last_action"] = "call"
        verb = "all-in" if p["all_in"] else "call"
        table["last_event"] = f"💰 {name} сделал {verb} на {paid}."
    elif action == "raise":
        if amount is None:
            return {"ok": False, "reason": "amount_required"}
        if p.get("acted"):  # неполный all-in не переоткрыл торги — ре-рейз запрещён
            return {"ok": False, "reason": "raise_not_reopened"}
        to_amount = int(amount)
        max_to = p["street_bet"] + p["stack"]
        min_to = table["current_bet"] + max(table.get("min_raise", BIG_BLIND), BIG_BLIND)
        if to_amount > max_to or to_amount <= table["current_bet"] or to_amount < min_to:
            return {"ok": False, "reason": "bad_raise"}
        delta = to_amount - p["street_bet"]
        _post(table, uid, delta)
        raise_size = to_amount - table["current_bet"]
        table["current_bet"] = to_amount
        table["min_raise"] = max(raise_size, BIG_BLIND)
        _reopen_action(table, uid)
        p["acted"] = True
        p["last_action"] = "raise"
        table["last_event"] = f"📈 {name} поднял ставку до {to_amount}."
    elif action == "allin":
        to_amount = p["street_bet"] + p["stack"]
        if to_amount <= p["street_bet"]:
            return {"ok": False, "reason": "no_chips"}
        old_bet = table["current_bet"]
        prev_min_raise = max(table.get("min_raise", BIG_BLIND), BIG_BLIND)
        _post(table, uid, p["stack"])
        if to_amount > old_bet:
            raise_size = to_amount - old_bet
            table["current_bet"] = to_amount
            # Полное повышение (>= min_raise) переоткрывает торги. НЕПОЛНЫЙ all-in
            # (короткий стек доставил меньше min_raise) — только докол: уже
            # походившие игроки НЕ получают права ре-рейза, лишь call/fold.
            if raise_size >= prev_min_raise:
                table["min_raise"] = raise_size
                _reopen_action(table, uid)
        p["acted"] = True
        p["last_action"] = "all-in"
        table["last_event"] = f"🚨 {name} пошёл all-in на {to_amount}."
    else:
        return {"ok": False, "reason": "unknown_action"}

    _advance_state(table, uid)
    return {"ok": True}


def _reopen_action(table: dict, raiser_uid: int) -> None:
    for uid in _actionable(table):
        if uid != raiser_uid:
            table["players"][uid]["acted"] = False


def _next_to_act(table: dict, after_uid: int | None = None) -> int | None:
    pool = _actionable(table)
    if not pool:
        return None
    if after_uid is None:
        return pool[0]
    return _next_from_index(table, _seat_index(table, after_uid), pool)


def _betting_round_complete(table: dict) -> bool:
    actors = _actionable(table)
    if not actors:
        return True
    for uid in actors:
        p = table["players"][uid]
        if not p.get("acted") or p.get("street_bet") != table.get("current_bet", 0):
            return False
    return True


def _advance_state(table: dict, acted_uid: int) -> None:
    contenders = _not_folded(table)
    if len(contenders) == 1:
        _award_uncontested(table, contenders[0])
        return

    # Торги закрыты? Только тогда решаем, что дальше. Проверять это ДО раннаута
    # критично: если кто-то в all-in, оставшийся игрок ещё должен ответить
    # (call/fold) — раунд НЕ закрыт, и раннаут украл бы у него ход (в heads-up
    # это лишало соперника all-in любого решения).
    if _betting_round_complete(table):
        # Больше ставить некому (все кроме одного в all-in) — докручиваем борд.
        if len(_actionable(table)) <= 1 and len(contenders) > 1:
            _runout_and_showdown(table)
        elif table.get("street") == "river":
            _showdown(table)
        else:
            _next_street(table)
        return

    table["current_turn"] = _next_to_act(table, acted_uid)


def _next_street(table: dict) -> None:
    street = table.get("street")
    if street == "preflop":
        table["board"].extend([table["deck"].pop(), table["deck"].pop(), table["deck"].pop()])
        table["street"] = "flop"
    elif street == "flop":
        table["board"].append(table["deck"].pop())
        table["street"] = "turn"
    elif street == "turn":
        table["board"].append(table["deck"].pop())
        table["street"] = "river"
    else:
        _showdown(table)
        return

    for uid in _not_folded(table):
        p = table["players"][uid]
        p["street_bet"] = 0
        p["acted"] = False
    table["current_bet"] = 0
    table["min_raise"] = BIG_BLIND

    dealer_uid = table["seats"][table["dealer_index"]]
    table["current_turn"] = _next_to_act(table, dealer_uid)
    table["last_event"] = f"🪄 Открыт {street_name(table['street'])}."

    if len(_actionable(table)) <= 1 and len(_not_folded(table)) > 1:
        _runout_and_showdown(table)


def _runout_and_showdown(table: dict) -> None:
    while len(table["board"]) < 5 and table["deck"]:
        if table["street"] == "preflop":
            table["board"].extend([table["deck"].pop(), table["deck"].pop(), table["deck"].pop()])
            table["street"] = "flop"
        else:
            table["board"].append(table["deck"].pop())
            table["street"] = "turn" if table["street"] == "flop" else "river"
    _showdown(table)


def _award_uncontested(table: dict, winner_uid: int) -> None:
    winner = table["players"][winner_uid]
    amount = table["pot"]
    winner["stack"] += amount
    table["pot"] = 0
    table["current_turn"] = None
    table["phase"] = "between_hands"
    table["last_event"] = f"🏆 {winner['name']} забрал банк {amount} без вскрытия."
    _post_hand_cleanup(table)


def _showdown(table: dict) -> None:
    contenders = _not_folded(table)
    board = list(table["board"])
    for uid in contenders:
        cards = table["players"][uid]["hole"] + board
        rank = best_hand(cards)
        table["players"][uid]["hand_rank"] = rank
        table["players"][uid]["showdown_name"] = HAND_NAMES[rank[0]]

    payouts = {uid: 0 for uid in table["players"]}
    lines = []
    for amount, elig in _side_pots(table):
        if not elig:
            continue
        best = max(table["players"][uid]["hand_rank"] for uid in elig)
        winners = [uid for uid in elig if table["players"][uid]["hand_rank"] == best]
        ordered = _ordered_from_button(table, winners)
        share = amount // len(ordered)
        rem = amount % len(ordered)
        for i, uid in enumerate(ordered):
            payouts[uid] += share + (1 if i < rem else 0)
        names = ", ".join(table["players"][uid]["name"] for uid in ordered)
        lines.append(f"• Банк {amount}: {names} ({table['players'][ordered[0]]['showdown_name']})")

    for uid, amount in payouts.items():
        if amount:
            table["players"][uid]["stack"] += amount

    table["pot"] = 0
    table["current_turn"] = None
    table["phase"] = "between_hands"
    table["last_event"] = "🏁 Вскрытие.\n" + "\n".join(lines)
    _post_hand_cleanup(table)


def _ordered_from_button(table: dict, ids: list[int]) -> list[int]:
    seats = table["seats"]
    start = table.get("dealer_index", -1)
    out = []
    for step in range(1, len(seats) + 1):
        idx = (start + step) % len(seats)
        uid = seats[idx]
        if uid in ids:
            out.append(uid)
    return out


def _side_pots(table: dict) -> list[tuple[int, list[int]]]:
    players = table["players"]
    levels = sorted({p["total_bet"] for p in players.values() if p.get("total_bet", 0) > 0})
    pots = []
    prev = 0
    for level in levels:
        contributors = [uid for uid, p in players.items() if p.get("total_bet", 0) >= level]
        amount = (level - prev) * len(contributors)
        elig = [uid for uid in contributors if players[uid].get("hole") and not players[uid].get("folded")]
        if amount > 0:
            pots.append((amount, elig))
        prev = level
    return pots


def _post_hand_cleanup(table: dict) -> None:
    for uid, p in table["players"].items():
        if p.get("stack", 0) <= 0:
            p["in_table"] = False
    if len(_participants(table)) < 2:
        _finish_tournament(table)


def _finish_tournament(table: dict) -> None:
    left = _participants(table)
    table["current_turn"] = None
    table["phase"] = "finished"
    prev = table.get("last_event", "")
    if left:
        winner = table["players"][left[0]]
        msg = f"👑 Турнир окончен. Победитель: {winner['name']} — {winner['stack']} фишек."
    else:
        msg = "👑 Турнир окончен. За столом не осталось игроков."
    # Не затираем итог последней раздачи (вскрытие/забрал банк), а дописываем.
    table["last_event"] = f"{prev}\n\n{msg}" if prev else msg


def disqualify_player(table: dict, uid: int) -> bool:
    p = table["players"].get(uid)
    if not p or not p.get("in_table"):
        return False
    p["in_table"] = False
    p["disqualified"] = True
    p["folded"] = True
    p["acted"] = True
    p["last_action"] = "dq"
    if table.get("current_turn") == uid:
        table["current_turn"] = _next_to_act(table, uid)
    contenders = _not_folded(table)
    if len(contenders) == 1:
        _award_uncontested(table, contenders[0])
    elif len(_participants(table)) < 2:
        _finish_tournament(table)
    return True


def street_name(street: str | None) -> str:
    return {
        "preflop": "префлоп",
        "flop": "флоп",
        "turn": "тёрн",
        "river": "ривер",
    }.get(street, "раздача")


def best_hand(cards: list[str]) -> tuple:
    return max(_eval_five(list(c)) for c in itertools.combinations(cards, 5))


def _eval_five(cards: list[str]) -> tuple:
    vals = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    counts = Counter(vals)
    freq = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    flush = len(set(suits)) == 1
    straight_high = _straight_high(vals)

    if flush and straight_high:
        return (8, straight_high)
    if freq[0][1] == 4:
        four = freq[0][0]
        kicker = max(v for v in vals if v != four)
        return (7, four, kicker)
    if freq[0][1] == 3 and freq[1][1] == 2:
        return (6, freq[0][0], freq[1][0])
    if flush:
        return (5, *vals)
    if straight_high:
        return (4, straight_high)
    if freq[0][1] == 3:
        three = freq[0][0]
        kickers = sorted((v for v in vals if v != three), reverse=True)
        return (3, three, *kickers)
    pairs = [v for v, c in freq if c == 2]
    if len(pairs) >= 2:
        hi, lo = sorted(pairs, reverse=True)[:2]
        kicker = max(v for v in vals if v not in (hi, lo))
        return (2, hi, lo, kicker)
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = sorted((v for v in vals if v != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *vals)


def _straight_high(vals: list[int]) -> int | None:
    uniq = sorted(set(vals), reverse=True)
    if 14 in uniq:
        uniq.append(1)
    run = 1
    best = None
    for i in range(1, len(uniq)):
        if uniq[i - 1] - 1 == uniq[i]:
            run += 1
            if run >= 5:
                best = uniq[i - 4]
        else:
            run = 1
    return best
