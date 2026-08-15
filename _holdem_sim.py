# -*- coding: utf-8 -*-
"""Стресс-тест логики holdem.py: гоняем много турниров со случайными
действиями и ловим исключения / расхождения в банке фишек."""
import random
import traceback

import holdem

TOTAL_CHIPS_PER_PLAYER = holdem.STARTING_STACK


def total_chips(table):
    return sum(p["stack"] for p in table["players"].values()) + table.get("pot", 0)


def random_action(table, uid, rnd):
    opts = holdem.allowed_actions(table, uid)
    if not opts:
        return None, None
    choices = []
    if opts.get("check"):
        choices.append(("check", None))
    if opts.get("call"):
        choices.append(("call", None))
    if opts.get("fold"):
        choices.append(("fold", None))
    for amt in opts.get("raise_to", []):
        choices.append(("raise", amt))
    if opts.get("all_in"):
        choices.append(("allin", None))
    if not choices:
        return None, None
    return rnd.choice(choices)


def play_one(seed):
    rnd = random.Random(seed)
    nplayers = rnd.randint(2, 6)
    table = holdem.new_table(host_id=1)
    for uid in range(1, nplayers + 1):
        holdem.add_player(table, uid, f"P{uid}")

    expected_total = nplayers * TOTAL_CHIPS_PER_PLAYER
    res = holdem.start_tournament(table, seed=seed)
    if not res.get("ok"):
        return

    guard = 0
    while table.get("phase") not in ("finished",):
        guard += 1
        if guard > 100000:
            raise RuntimeError(f"seed={seed}: похоже на зависание (100k итераций)")

        # проверка сохранения фишек
        if table.get("phase") == "playing":
            got = total_chips(table)
            if got != expected_total:
                raise AssertionError(
                    f"seed={seed}: банк+стеки={got}, ожидалось {expected_total}"
                )

        if table.get("phase") == "playing":
            uid = table.get("current_turn")
            if uid is None:
                # нет хода, но раздача не закрыта — двигаем состояние принудительно
                raise AssertionError(
                    f"seed={seed}: phase=playing, но current_turn=None"
                )
            action, amount = random_action(table, uid, rnd)
            if action is None:
                raise AssertionError(
                    f"seed={seed}: нет доступных действий для current_turn={uid}"
                )
            r = holdem.apply_action(table, uid, action, amount)
            if not r.get("ok"):
                raise AssertionError(
                    f"seed={seed}: apply_action отклонил ход {action}/{amount}: {r}"
                )
        elif table.get("phase") == "between_hands":
            holdem.begin_hand(table, seed=rnd.randint(0, 10**9))

    # финальная проверка
    got = total_chips(table)
    if got != expected_total:
        raise AssertionError(
            f"seed={seed}: ИТОГ банк+стеки={got}, ожидалось {expected_total}"
        )


def main():
    fails = 0
    for seed in range(3000):
        try:
            play_one(seed)
        except Exception as e:
            fails += 1
            print("=" * 60)
            print(f"СБОЙ seed={seed}: {type(e).__name__}: {e}")
            traceback.print_exc()
            if fails >= 15:
                print("Слишком много сбоев, останавливаюсь.")
                break
    if fails == 0:
        print("OK: 3000 турниров без ошибок и без потери фишек.")
    else:
        print(f"Всего сбоев: {fails}")


if __name__ == "__main__":
    main()
