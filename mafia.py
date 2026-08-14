# -*- coding: utf-8 -*-
"""
Движок игры «Мафия» — ЧИСТАЯ логика (без aiogram), поэтому легко тестируется.

Хэндлеры/лобби/таймеры/рассылка в ЛС живут в bot.py; здесь — только правила:
раздача ролей, подсчёт живых, условие победы, разрешение ночи и голосования.

Роли:
  mafia     — ночью вместе выбирают жертву; днём маскируются под мирных.
  commissar — ночью проверяет одного игрока: мафия он или нет.
  doctor    — ночью лечит одного (можно себя): спасает от убийства мафии.
  civilian  — мирный житель, только днём голосует.

Состояние игрока (в bot.py): {"name": str, "role": role, "alive": bool}.
"""

import random
from collections import Counter

ROLE_MAFIA = "mafia"
ROLE_COMMISSAR = "commissar"
ROLE_DOCTOR = "doctor"
ROLE_CIVILIAN = "civilian"

# role -> (эмодзи, название, описание для ЛС).
ROLE_INFO = {
    ROLE_MAFIA: ("🔫", "Мафия",
                 "Ночью вместе с подельниками выбираешь жертву. "
                 "Днём притворяйся мирным и уводи подозрения."),
    ROLE_COMMISSAR: ("🕵️", "Комиссар",
                     "Ночью проверяешь одного игрока — узнаёшь, мафия он или нет. "
                     "Днём аккуратно веди мирных к правде."),
    ROLE_DOCTOR: ("💉", "Доктор",
                  "Ночью лечишь одного игрока (можно себя). Если вылечишь того, "
                  "кого этой ночью выбрала мафия, — он выживет."),
    ROLE_CIVILIAN: ("👤", "Мирный житель",
                    "Особых умений нет. Днём обсуждай, вычисляй мафию и голосуй."),
}

MIN_PLAYERS = 4          # минимум для старта
MAX_PLAYERS = 20         # разумный потолок


def assign_roles(user_ids, seed=None) -> dict:
    """Раздать роли по числу игроков. Возвращает {uid: role}.

    Мафии ~25% (минимум 1). Затем по одному комиссару и доктору (если хватает
    людей), остальные — мирные.
    """
    ids = list(user_ids)
    rng = random.Random(seed)
    rng.shuffle(ids)
    n = len(ids)
    mafia_count = max(1, n // 4)

    roles = {}
    i = 0
    for _ in range(mafia_count):
        roles[ids[i]] = ROLE_MAFIA
        i += 1
    if i < n:                       # комиссар
        roles[ids[i]] = ROLE_COMMISSAR
        i += 1
    if i < n:                       # доктор
        roles[ids[i]] = ROLE_DOCTOR
        i += 1
    while i < n:                    # остальные — мирные
        roles[ids[i]] = ROLE_CIVILIAN
        i += 1
    return roles


def alive_ids(players) -> list:
    return [uid for uid, p in players.items() if p.get("alive")]


def alive_by_role(players, role) -> list:
    return [uid for uid, p in players.items()
            if p.get("alive") and p.get("role") == role]


def count_alive(players):
    """(живых мафий, живых не-мафий)."""
    mafia = sum(1 for p in players.values()
                if p.get("alive") and p.get("role") == ROLE_MAFIA)
    others = sum(1 for p in players.values()
                 if p.get("alive") and p.get("role") != ROLE_MAFIA)
    return mafia, others


def check_win(players):
    """'peace' — мирные победили, 'mafia' — мафия победила, None — игра идёт."""
    mafia, others = count_alive(players)
    if mafia == 0:
        return "peace"
    if mafia >= others:
        return "mafia"
    return None


def pick_mafia_target(mafia_votes, seed=None):
    """По голосам мафии {mafia_uid: target_uid} выбрать жертву.

    Побеждает цель с наибольшим числом голосов; при равенстве — случайно
    среди лидеров. Нет голосов -> None (в эту ночь мафия никого не убивает).
    """
    votes = [t for t in mafia_votes.values() if t is not None]
    if not votes:
        return None
    counts = Counter(votes)
    top = max(counts.values())
    leaders = [t for t, c in counts.items() if c == top]
    return random.Random(seed).choice(leaders)


def resolve_night(players, mafia_target, doctor_target):
    """Кого мафия реально убила ночью (или None).

    Доктор спасает, если лечил ровно ту же цель. Цель должна быть живой.
    """
    if mafia_target is None:
        return None
    if mafia_target == doctor_target:      # доктор успел вылечить
        return None
    if players.get(mafia_target, {}).get("alive"):
        return mafia_target
    return None


def tally_votes(votes):
    """Голоса дня {voter_uid: target_uid} -> (казнённый_uid | None, распределение).

    'skip'/None не считаются целями. При равенстве лидеров — никого не казнят
    (None), чтобы случайно не выгнать невиновного. Возвращает (uid|None, counts).
    """
    real = [t for t in votes.values() if t not in (None, "skip")]
    counts = Counter(real)
    if not counts:
        return None, counts
    top = max(counts.values())
    leaders = [t for t, c in counts.items() if c == top]
    if len(leaders) != 1:
        return None, counts
    return leaders[0], counts
