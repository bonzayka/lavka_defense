# 🂡 Веб-покер — Telegram Mini App

Веб-версия Texas Hold'em поверх той же логики, что и групповой бот (`holdem.py`).
Открывается кнопкой в личке бота (`/pokerapp`), играется в браузере Telegram,
реалтайм — по WebSocket.

## Структура
- `server.py` — бэкенд на FastAPI: раздаёт страницу стола, держит игровые
  комнаты в памяти, валидирует Telegram `initData`, гоняет ходы по WebSocket
  и рассылает состояние (каждому — только его карты).
- `static/index.html` — одностраничный стол: `telegram-web-app.js`, отрисовка
  стола/карт и кнопки Fold / Check / Call / Raise / All-in.
- `_smoke.py` — смоук-тест бэкенда (подпись initData, сериализация, раздача,
  HTTP и WebSocket).

## Локальный запуск (в браузере, без Telegram)
```bash
WEBAPP_DEV=1 venv/bin/uvicorn webapp.server:app --port 8080
# открой http://localhost:8080/?room=test  (в dev-режиме вход без подписи)
```

## Продакшн (на VPS вместе с ботом)
1. Включи в окружении:
   ```bash
   export WEBAPP_ENABLED=1
   export WEBAPP_URL="https://poker.твой-домен"   # публичный HTTPS
   ```
   Тогда `bot.py` сам поднимет сервер в своём процессе на `WEBAPP_PORT` (8080).
2. Telegram открывает Mini App **только по HTTPS** — поставь reverse-proxy с TLS:
   ```
   # Caddy
   poker.твой-домен {
       reverse_proxy 127.0.0.1:8080
   }
   ```
3. Пропиши тот же домен боту у **@BotFather** (`/setdomain` или `/setmenubutton`).
4. В Telegram напиши боту в ЛС `/pokerapp` — появится кнопка «Открыть покер-стол».
   Общий стол по коду: `/pokerapp lobby7` (у всех должен быть один код комнаты).

## Тесты
```bash
venv/bin/python -m webapp._smoke      # бэкенд Mini App
venv/bin/python _holdem_sim.py        # 3000 турниров логики holdem.py
```

## Ограничения каркаса (осознанные)
- Комнаты живут в памяти процесса (перезапуск — столы сбрасываются). Для
  постоянных столов подойдёт вынос состояния в Redis/БД.
- Веб-столы независимы от групповых `/holdem` (отдельные комнаты) — так проще и
  безопаснее для уже работающего бота.
- Нет таймера хода в веб-версии (в отличие от группового стола) — можно добавить
  по аналогии с `_holdem_turn_timer` в `bot.py`.
