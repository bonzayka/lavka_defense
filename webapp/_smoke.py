# -*- coding: utf-8 -*-
"""Смоук-тест бэкенда Mini App: подпись initData, сериализация вида игрока,
полная раздача через логику сервера и HTTP-роуты. Запуск:
    venv\\Scripts\\python.exe -m webapp._smoke
"""
import hashlib
import hmac
import json
import time
import urllib.parse

import holdem
from webapp import server as S


def test_initdata():
    token = "123456:TESTTOKEN"
    user = json.dumps({"id": 123, "first_name": "Al", "username": "al"}, separators=(",", ":"))
    params = {"user": user, "auth_date": str(int(time.time())), "query_id": "AAA"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    init = urllib.parse.urlencode({**params, "hash": h})

    ok = S.verify_init_data(init, token)
    assert ok and ok["id"] == 123 and ok["name"] == "Al", f"валидная подпись не прошла: {ok}"

    tampered = init.replace("%22id%22%3A123", "%22id%22%3A999")
    assert S.verify_init_data(tampered, token) is None, "подделанные данные приняты!"
    assert S.verify_init_data(init, "wrong:token") is None, "чужой токен принят!"
    print("[ok] initData: валидная принята, подделка/чужой токен отклонены")


def test_full_hand():
    room = S.Room("test", host_id=101)
    t = room.table
    for uid, nm in [(101, "Alice"), (202, "Bob"), (303, "Carol")]:
        holdem.add_player(t, uid, nm)
    assert holdem.start_tournament(t)["ok"], "турнир не стартовал"

    # Проверяем, что каждый видит только свои карты в начале раздачи.
    for viewer in (101, 202, 303):
        view = S.serialize(t, viewer)
        for seat in view["seats"]:
            if seat["uid"] == viewer:
                assert all(c != "🂠" for c in seat["cards"]), "свои карты скрыты!"
            else:
                assert seat["cards"] in ([], ["🂠", "🂠"]), f"видны чужие карты: {seat}"
    print("[ok] serialize: игрок видит свои карты, чужие скрыты рубашкой")

    # Играем раздачу до конца, каждый раз беря первое доступное действие.
    guard = 0
    while t["phase"] == "playing":
        guard += 1
        assert guard < 500, "раздача зациклилась"
        uid = t["current_turn"]
        assert uid is not None, "playing, но current_turn=None"
        view = S.serialize(t, uid)
        opts = view["you"]["options"]
        if opts.get("check"):
            holdem.apply_action(t, uid, "check")
        elif opts.get("call"):
            holdem.apply_action(t, uid, "call")
        else:
            holdem.apply_action(t, uid, "fold")

    assert t["phase"] in ("between_hands", "finished"), f"неожиданная фаза: {t['phase']}"
    # Финальный вид не должен падать ни у кого (в т.ч. у вылетевших).
    for viewer in (101, 202, 303):
        S.serialize(t, viewer)
    print(f"[ok] полная раздача сыграна без ошибок, фаза={t['phase']}, событие: "
          f"{t['last_event'].splitlines()[0]}")


def test_http():
    from fastapi.testclient import TestClient
    with TestClient(S.app) as client:
        r = client.get("/health")
        assert r.status_code == 200 and r.json().get("ok"), r.text
        r = client.get("/")
        assert r.status_code == 200 and "telegram-web-app.js" in r.text, "страница стола не отдалась"
    print("[ok] HTTP: /health и / (страница стола) отвечают 200")


def test_ws():
    """Живой прогон WebSocket: авторизация (dev), join x2, start, один ход."""
    import os
    os.environ["WEBAPP_DEV"] = "1"
    S.WEBAPP_DEV = True
    from fastapi.testclient import TestClient

    def drain(ws):
        st = None
        for _ in range(6):
            st = ws.receive_json()
            if st.get("type") == "state":
                break
        return st

    with TestClient(S.app) as client:
        with client.websocket_connect("/ws") as w1, client.websocket_connect("/ws") as w2:
            w1.send_json({"type": "auth", "initData": "", "room": "wstest", "dev_uid": 11, "dev_name": "A"})
            drain(w1)
            w2.send_json({"type": "auth", "initData": "", "room": "wstest", "dev_uid": 22, "dev_name": "B"})
            drain(w2)
            w1.send_json({"type": "join"}); drain(w1); drain(w2)
            w2.send_json({"type": "join"}); drain(w1); drain(w2)
            w1.send_json({"type": "start"})
            s1 = drain(w1); drain(w2)
            assert s1["phase"] == "playing", f"после start фаза {s1['phase']}"
    print("[ok] WebSocket: auth+join x2+start отработали, стол в игре")


if __name__ == "__main__":
    test_initdata()
    test_full_hand()
    test_http()
    test_ws()
    print("\nВСЁ ЗЕЛЁНОЕ ✅  бэкенд Mini App работает.")
