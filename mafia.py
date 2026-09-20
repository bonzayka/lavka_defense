# -*- coding: utf-8 -*-
"""
Движок игры «Мафия» — ЧИСТАЯ логика (без aiogram), поэтому легко тестируется.

Хэндлеры/лобби/таймеры/рассылка в ЛС живут в bot.py; здесь — только правила:
раздача ролей, подсчёт живых, условие победы, разрешение ночи и голосования.

Роли:
  mafia     — ночью вместе выбирают жертву; днём маскируются под мирных.
  don       — глава мафии: стреляет с бандой и ночью ищет Комиссара.
  commissar — ночью проверяет одного игрока: мафия он или нет.
  doctor    — ночью лечит одного (можно себя): спасает от гибели.
  mistress  — любовница/красотка: ночью блокирует действие выбранного игрока.
  maniac    — маньяк-одиночка: каждую ночь убивает любого; играет сам за себя.
  civilian  — мирный житель, только днём обсуждает и голосует.

Состояние игрока (в bot.py): {"name": str, "role": role, "alive": bool}.
"""

import random
from collections import Counter

ROLE_MAFIA = "mafia"
ROLE_DON = "don"
ROLE_COMMISSAR = "commissar"
ROLE_DOCTOR = "doctor"
ROLE_MISTRESS = "mistress"
ROLE_MANIAC = "maniac"
ROLE_CIVILIAN = "civilian"

MAFIA_ROLES = {ROLE_MAFIA, ROLE_DON}
PEACE_ROLES = {ROLE_COMMISSAR, ROLE_DOCTOR, ROLE_MISTRESS, ROLE_CIVILIAN}

# role -> (эмодзи, название, описание для ЛС).
ROLE_INFO = {
    ROLE_MAFIA: ("🔫", "Мафия",
                 "Ночью вместе с подельниками выбираешь жертву. "
                 "Днём притворяйся мирным и уводи подозрения."),
    ROLE_DON: ("👑", "Дон Мафии",
               "Глава мафии! Ночью стреляешь вместе с бандой, а также ищешь "
               "Комиссара среди игроков. Днём маскируйся под мирного."),
    ROLE_COMMISSAR: ("🕵️", "Комиссар",
                     "Ночью проверяешь одного игрока — узнаёшь, мафия он или нет. "
                     "Днём аккуратно веди мирных к правде."),
    ROLE_DOCTOR: ("💉", "Доктор",
                  "Ночью лечишь одного игрока (можно себя). Если вылечишь того, "
                  "кого этой ночью атаковали, — он выживет."),
    ROLE_MISTRESS: ("💋", "Любовница",
                    "Ночью выбираешь игрока и проводишь с ним ночь, блокируя "
                    "любое его ночное действие (лечение, проверку или выстрел)."),
    ROLE_MANIAC: ("🔪", "Маньяк",
                  "Одиночка без подельников! Каждую ночь убиваешь любого игрока. "
                  "Твоя цель — победить, оставшись в живых в одиночку."),
    ROLE_CIVILIAN: ("👤", "Мирный житель",
                    "Особых умений нет. Днём обсуждай, вычисляй злодеев и голосуй."),
}

MIN_PLAYERS = 4          # минимум для старта
MAX_PLAYERS = 20         # разумный потолок


def is_mafia(role: str) -> bool:
    """Относится ли роль к мафии (включая Дона)."""
    return role in MAFIA_ROLES


def assign_roles(user_ids, seed=None) -> dict:
    """Раздать роли по числу игроков. Возвращает {uid: role}.

    4-5 игроков: 1 мафия, 1 комиссар, 1 доктор, остальные мирные.
    6 игроков: 1 дон, 1 комиссар, 1 доктор, 1 любовница, 2 мирных.
    7 игроков: 1 дон, 1 мафия, 1 комиссар, 1 доктор, 1 любовница, 2 мирных.
    8+ игроков: 1 дон, 1 мафия, 1 комиссар, 1 доктор, 1 любовница, 1 маньяк, мирные.
    12+ игроков: +1 мафия.
    """
    ids = list(user_ids)
    rng = random.Random(seed)
    rng.shuffle(ids)
    n = len(ids)
    roles = {}
    i = 0

    if n >= 6:
        roles[ids[i]] = ROLE_DON
        i += 1
    else:
        roles[ids[i]] = ROLE_MAFIA
        i += 1

    if n >= 7:
        roles[ids[i]] = ROLE_MAFIA
        i += 1

    if n >= 12:
        roles[ids[i]] = ROLE_MAFIA
        i += 1

    if i < n:
        roles[ids[i]] = ROLE_COMMISSAR
        i += 1
    if i < n:
        roles[ids[i]] = ROLE_DOCTOR
        i += 1
    if n >= 6 and i < n:
        roles[ids[i]] = ROLE_MISTRESS
        i += 1
    if n >= 8 and i < n:
        roles[ids[i]] = ROLE_MANIAC
        i += 1
    while i < n:
        roles[ids[i]] = ROLE_CIVILIAN
        i += 1
    return roles


def alive_ids(players) -> list:
    return [uid for uid, p in players.items() if p.get("alive")]


def alive_by_role(players, role) -> list:
    return [uid for uid, p in players.items()
            if p.get("alive") and p.get("role") == role]


def alive_mafia_ids(players) -> list:
    """Все живые мафиози (обычная мафия + дон)."""
    return [uid for uid, p in players.items()
            if p.get("alive") and p.get("role") in MAFIA_ROLES]


def count_alive(players):
    """(живых мафий, живых не-мафий). Сохраняет обратную совместимость."""
    mafia = sum(1 for p in players.values()
                if p.get("alive") and p.get("role") in MAFIA_ROLES)
    others = sum(1 for p in players.values()
                 if p.get("alive") and p.get("role") not in MAFIA_ROLES)
    return mafia, others


def check_win(players):
    """'peace' — мирные победили, 'mafia' — мафия победила, 'maniac' — маньяк победил, None — игра идёт."""
    mafia_cnt = sum(1 for p in players.values() if p.get("alive") and p.get("role") in MAFIA_ROLES)
    maniac_cnt = sum(1 for p in players.values() if p.get("alive") and p.get("role") == ROLE_MANIAC)
    others_cnt = sum(1 for p in players.values() if p.get("alive") and p.get("role") not in MAFIA_ROLES and p.get("role") != ROLE_MANIAC)
    total_alive = mafia_cnt + maniac_cnt + others_cnt

    if total_alive == 0:
        return "peace"

    # Мафия и маньяк мертвы -> мирные победили
    if mafia_cnt == 0 and maniac_cnt == 0:
        return "peace"

    # Маньяк остался один либо 1-на-1 с последним выжившим (мафия при этом мертва)
    if mafia_cnt == 0 and maniac_cnt > 0 and others_cnt <= 1:
        return "maniac"

    # Маньяк в дуэли 1-на-1 с последней мафией побеждает маньяк
    if others_cnt == 0 and mafia_cnt == 1 and maniac_cnt == 1:
        return "maniac"

    # Маньяк мёртв, а мафия составляет половину или более оставшихся
    if maniac_cnt == 0 and mafia_cnt >= others_cnt:
        return "mafia"

    return None


def pick_mafia_target(mafia_votes, seed=None):
    """По голосам мафии {mafia_uid: target_uid} выбрать жертву.

    Побеждает цель с наибольшим числом голосов; при равенстве — случайно
    среди лидеров. Нет голосов -> None.
    """
    votes = [t for t in mafia_votes.values() if t is not None]
    if not votes:
        return None
    counts = Counter(votes)
    top = max(counts.values())
    leaders = [t for t, c in counts.items() if c == top]
    return random.Random(seed).choice(leaders)


def resolve_night(players, mafia_target, doctor_target, maniac_target=None, mistress_target=None, return_list=False):
    """Разрешение событий ночи с учётом Любовницы, Доктора, Мафии и Маньяка.

    Если return_list=True -> возвращает list[int] всех погибших этой ночью.
    Если return_list=False -> для обратной совместимости возвращает одиночный uid или None.
    """
    eff_doctor = doctor_target
    eff_maniac = maniac_target
    eff_mafia = mafia_target

    # Блокировка Любовницы:
    if mistress_target is not None and players.get(mistress_target, {}).get("alive"):
        m_role = players[mistress_target].get("role")
        if m_role == ROLE_DOCTOR:
            eff_doctor = None
        elif m_role == ROLE_MANIAC:
            eff_maniac = None
        elif m_role in MAFIA_ROLES:
            alive_mafia = [uid for uid, p in players.items() if p.get("alive") and p.get("role") in MAFIA_ROLES]
            if len(alive_mafia) <= 1:
                eff_mafia = None

    killed = []

    # 1. Жертва мафии
    if eff_mafia is not None and players.get(eff_mafia, {}).get("alive"):
        if eff_mafia != eff_doctor:
            killed.append(eff_mafia)

    # 2. Жертва маньяка
    if eff_maniac is not None and players.get(eff_maniac, {}).get("alive"):
        if eff_maniac != eff_doctor and eff_maniac not in killed:
            killed.append(eff_maniac)

    if return_list:
        return killed
    return killed[0] if killed else None


def tally_votes(votes):
    """Голоса дня {voter_uid: target_uid} -> (казнённый_uid | None, распределение).

    'skip'/None не считаются целями. При равенстве лидеров — никого не казнят
    (None). Возвращает (uid|None, counts).
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
