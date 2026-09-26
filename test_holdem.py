# -*- coding: utf-8 -*-
"""
Регрессионные тесты правил Texas Holdem (holdem.py).

Стресс-тест _holdem_sim.py ловит только утечку фишек и зависания и НЕ видит
нарушений правил (украденный ход, незаконный ре-рейз): деньги сходятся, а
правило нарушено. Эти тесты проверяют КОРРЕКТНОСТЬ правил и стерегут от
регрессии двух найденных багов.

Запуск:  python test_holdem.py   (0 = все прошли)
"""

import holdem as h


def _rank(cards):
    return h.best_hand(cards.split())


def _name(cards):
    return h.HAND_NAMES[_rank(cards)[0]]


def test_hand_evaluator():
    cases = [
        ("Ah Kh Qh Jh Th 2c 3d", "стрит-флеш"),
        ("9h 8h 7h 6h 5h Ac Kd", "стрит-флеш"),
        ("Ac Ad Ah As Kd 2c 3d", "каре"),
        ("Kc Kd Kh 2s 2d 5c 9h", "фулл-хаус"),
        ("Ah 2h 5h 9h Jh 3c 4d", "флеш"),
        ("As 2d 3c 4h 5s 9d Kc", "стрит"),
        ("Ah Ad As 7c 8d 9h Tc", "сет"),
        ("Ah Ad Kc Kd 5s 6c 7h", "две пары"),
        ("Ah Ad 5c 8d 9h Jc 2s", "пара"),
        ("Ah Kd 9c 7s 5h 3d 2c", "старшая карта"),
    ]
    for cards, exp in cases:
        assert _name(cards) == exp, f"{cards}: {_name(cards)} != {exp}"


def test_hand_strength_order():
    assert _rank("Ah Kh Qh Jh Th 2c 3d") > _rank("Ac Ad Ah As Kd 2c 3d")
    assert _rank("2s 3d 4c 5h 6s 9d Kc") > _rank("As 2d 3c 4h 5s 9d Kc")
    assert _rank("Ah 9h 5h 3h 2h 4c 4d") > _rank("Kh 9h 5h 3h 2h 4c 4d")
    assert _rank("Ah Ad 2c 2d 9h 5c 7s") > _rank("Kh Kd Qc Qd 9h 5c 7s")


def test_wheel_is_lowest_straight():
    r = _rank("As 2d 3c 4h 5s 9d Kc")
    assert r[0] == 4 and r[1] == 5, f"колесо должно быть 5-high стритом, got {r}"


def _headsup_all_in_preflop(seed):
    t = h.new_table(1)
    h.add_player(t, 1, "A")
    h.add_player(t, 2, "B")
    h.start_tournament(t, seed=seed)
    return t


def test_bug1_opponent_acts_vs_allin():
    t = _headsup_all_in_preflop(seed=1)
    first = t["current_turn"]
    other = 2 if first == 1 else 1
    h.apply_action(t, first, "allin")
    assert t["current_turn"] == other, "соперник all-in НЕ получил ход"
    assert t["phase"] == "playing", "раздача завершилась до ответа соперника"
    assert t["board"] == [], "борд открыт до ответа соперника"
    r = h.apply_action(t, other, "call")
    assert r["ok"]
    assert len(t["board"]) == 5, "после закрытия торгов борд не докручен"
    assert t["phase"] in ("between_hands", "finished")


def test_multiway_allin_runs_out():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=7)
    guard = 0
    while t["phase"] == "playing" and guard < 20:
        guard += 1
        u = t["current_turn"]
        o = h.allowed_actions(t, u)
        act = "allin" if o.get("all_in") else ("call" if o.get("call") else "check")
        assert h.apply_action(t, u, act)["ok"]
    assert t["phase"] == "finished"
    assert len(t["board"]) == 5
    assert sum(p["stack"] for p in t["players"].values()) + t["pot"] == 150_000


def _raiser_after_incomplete_allin(seed):
    t = h.new_table(1)
    for i in (1, 2, 3, 4):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=seed)
    u = t["current_turn"]
    h.apply_action(t, u, "raise", 3000)
    raiser = u
    u = t["current_turn"]
    p = t["players"][u]
    p["stack"] = 3400 - p["street_bet"]
    h.apply_action(t, u, "allin")
    for _ in range(6):
        u = t["current_turn"]
        if u == raiser:
            break
        o = h.allowed_actions(t, u)
        if o.get("call"):
            h.apply_action(t, u, "call")
        elif o.get("check"):
            h.apply_action(t, u, "check")
        else:
            break
    return t, raiser


def test_bug2_incomplete_allin_no_reraise():
    t, raiser = _raiser_after_incomplete_allin(seed=2)
    assert t["current_turn"] == raiser, "круг не вернулся к рейзеру"
    o = h.allowed_actions(t, raiser)
    assert o.get("raise_to") == [], "неполный all-in незаконно открыл ре-рейз"
    assert o.get("call"), "call против доклада должен быть доступен"
    r = h.apply_action(t, raiser, "raise", 7400)
    assert not r["ok"] and r["reason"] == "raise_not_reopened"


def test_full_raise_reopens():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=5)
    u = t["current_turn"]
    h.apply_action(t, u, "raise", 3000)
    r1 = u
    u = t["current_turn"]
    h.apply_action(t, u, "raise", 6000)
    for _ in range(5):
        u = t["current_turn"]
        if u == r1:
            break
        o = h.allowed_actions(t, u)
        if o.get("call"):
            h.apply_action(t, u, "call")
        else:
            break
    o = h.allowed_actions(t, r1)
    assert o.get("raise_to"), "полный ре-рейз должен снова открывать рейз рейзеру"


def test_min_raise_preflop():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=2)
    o = h.allowed_actions(t, t["current_turn"])
    assert o["raise_to"] and o["raise_to"][0] == 2000, o.get("raise_to")


def test_bb_option():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=13)
    for _ in range(10):
        u = t["current_turn"]
        o = h.allowed_actions(t, u)
        if o.get("check"):
            return
        h.apply_action(t, u, "call" if o.get("call") else "check")
    assert False, "BB не получил опцию check"


def test_postflop_order():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    h.start_tournament(t, seed=11)
    guard = 0
    while t["street"] == "preflop" and t["phase"] == "playing" and guard < 20:
        guard += 1
        u = t["current_turn"]
        o = h.allowed_actions(t, u)
        h.apply_action(t, u, "call" if o.get("call") else "check")
    di = t["dealer_index"]
    seats = t["seats"]
    assert t["current_turn"] == seats[(di + 1) % len(seats)]


def test_side_pot_conservation():
    t = h.new_table(1)
    for i in (1, 2, 3):
        h.add_player(t, i, f"P{i}")
    t["players"][1]["stack"] = 1000
    t["players"][2]["stack"] = 50_000
    t["players"][3]["stack"] = 50_000
    total = 101_000
    h.start_tournament(t, seed=21)
    guard = 0
    while t["phase"] == "playing" and guard < 40:
        guard += 1
        u = t["current_turn"]
        o = h.allowed_actions(t, u)
        act = "allin" if o.get("all_in") else ("call" if o.get("call") else "check")
        h.apply_action(t, u, act)
    assert sum(p["stack"] for p in t["players"].values()) + t["pot"] == total


def test_eliminated_player_no_ghost_bet_or_stack():
    t = h.new_table(1)
    h.add_player(t, 1, "Hero")
    h.add_player(t, 2, "Opponent")
    t["players"][2]["stack"] = 2000
    h.start_tournament(t, seed=42)
    # Hero raises to 3000, Opponent calls (all-in with 2000 total)
    h.apply_action(t, 1, "raise", 3000)
    h.apply_action(t, 2, "call")
    assert t["phase"] in ("between_hands", "finished")
    # All street_bets must be 0 after showdown
    for p in t["players"].values():
        assert p["street_bet"] == 0, f"street_bet lingering: {p['street_bet']}"
    # In seed 42, Hero wins, Opponent has 0 stack and is eliminated
    opp = t["players"][2]
    if opp["stack"] == 0:
        assert not opp["in_table"], "busted player must not be in_table"
        assert t["phase"] == "finished", "tournament must finish when 1 player left"
        assert opp["street_bet"] == 0


def test_incomplete_allin_cannot_bypass_raise_lock():
    import copy
    t, uid = _raiser_after_incomplete_allin(seed=2)
    assert not h.allowed_actions(t, uid)["all_in"]
    before = copy.deepcopy(t)
    assert h.apply_action(t, uid, "allin") == {"ok": False, "reason": "raise_not_reopened"}
    assert t == before


def test_cumulative_short_allins_reopen_betting():
    t = h.new_table(1)
    for uid in range(1, 5):
        h.add_player(t, uid, str(uid))
    h.start_tournament(t, seed=2)
    raiser = t["current_turn"]
    assert h.apply_action(t, raiser, "raise", 3000)["ok"]
    for amount in (4000, 5000):
        uid = t["current_turn"]
        t["players"][uid]["stack"] = amount - t["players"][uid]["street_bet"]
        assert h.apply_action(t, uid, "allin")["ok"]
    assert h.apply_action(t, t["current_turn"], "call")["ok"]
    assert t["current_turn"] == raiser
    assert h.allowed_actions(t, raiser)["min_raise_to"] == 7000
    assert h.apply_action(t, raiser, "raise", 7000)["ok"]


def test_short_big_blind_keeps_nominal_bet_multiway():
    t = h.new_table(1)
    for uid in range(1, 5):
        h.add_player(t, uid, str(uid))
    t["players"][3]["stack"] = 200
    h.start_tournament(t, seed=4)
    assert t["current_bet"] == 1000
    assert h.allowed_actions(t, t["current_turn"])["min_raise_to"] == 2000


def test_heads_up_short_blind_runs_out_without_dry_bet():
    t = h.new_table(1)
    h.add_player(t, 1, "A")
    h.add_player(t, 2, "B")
    t["players"][2]["stack"] = 200
    h.start_tournament(t, seed=4)
    assert t["phase"] in ("between_hands", "finished")
    assert len(t["board"]) == 5
    assert sum(p["stack"] for p in t["players"].values()) == 50200


def test_no_raise_into_dry_side_pot():
    t = h.new_table(1)
    for uid in (1, 2):
        h.add_player(t, uid, str(uid))
    t["players"][1]["stack"] = 3000
    h.start_tournament(t, seed=2)
    assert h.apply_action(t, 1, "allin")["ok"]
    opts = h.allowed_actions(t, 2)
    assert opts["call"] and not opts["all_in"] and not opts["raise_to"]
    assert not h.apply_action(t, 2, "allin")["ok"]


def test_custom_blinds_determine_raise():
    t = h.new_table(1)
    t.update(small_blind=25, big_blind=50)
    for uid in (1, 2, 3):
        h.add_player(t, uid, str(uid))
    h.start_tournament(t, seed=2)
    assert h.allowed_actions(t, t["current_turn"])["min_raise_to"] == 100
    while t["street"] == "preflop":
        uid = t["current_turn"]
        opts = h.allowed_actions(t, uid)
        assert h.apply_action(t, uid, "call" if opts["call"] else "check")["ok"]
    assert h.allowed_actions(t, t["current_turn"])["min_raise_to"] == 50


def test_invalid_raise_never_mutates_state():
    import copy
    t = _headsup_all_in_preflop(3)
    for amount in (True, 2000.5, "3000", -2, 999999, None):
        before = copy.deepcopy(t)
        assert not h.apply_action(t, t["current_turn"], "raise", amount)["ok"]
        assert before == t


def test_disqualification_does_not_finish_a_live_allin_hand():
    t = h.new_table(1)
    for uid in (1, 2, 3):
        h.add_player(t, uid, str(uid))
    t["players"][1]["stack"] = 3000
    h.start_tournament(t, seed=5)
    assert h.apply_action(t, 1, "allin")["ok"]
    assert h.disqualify_player(t, 2)
    assert t["phase"] == "playing"
    assert t["current_turn"] == 3
    assert h.apply_action(t, 3, "call")["ok"]
    assert len(t["board"]) == 5
    assert sum(p["stack"] for p in t["players"].values()) == 103000


def test_folded_bets_clear_on_next_street():
    t = h.new_table(1)
    for uid in (1, 2, 3):
        h.add_player(t, uid, str(uid))
    h.start_tournament(t, seed=5)
    assert h.apply_action(t, 1, "call")["ok"]
    assert h.apply_action(t, 2, "fold")["ok"]
    assert h.apply_action(t, 3, "check")["ok"]
    assert t["street"] == "flop"
    assert t["players"][2]["street_bet"] == 0
    assert t["players"][2]["total_bet"] == 500


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            fails += 1
            print(f"ERR  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\nИтого: {len(tests)} тестов, {fails} провалов.")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
