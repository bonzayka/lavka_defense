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
- `static/lounge.css` — адаптивное оформление стола; `static/app.js` —
  управление, переподключение и горячие клавиши F / C / R.
- `_smoke.py` — смоук-тест бэкенда (подпись initData, сериализация, раздача,
  HTTP и WebSocket).
- `test_regressions.py` — проверки прав, скрытых карт, таймеров и ошибочных запросов.

## Локальный запуск (в браузере, без Telegram)
```bash
WEBAPP_DEV=1 venv/bin/uvicorn webapp.server:app --port 8080
# открой http://localhost:8080/?room=test  (в dev-режиме вход без подписи)
```

Windows / PowerShell (из корня проекта):
```powershell
$env:WEBAPP_DEV = "1"
python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8080
```
Откройте `http://127.0.0.1:8080/?room=test&dev_uid=101` и вторую вкладку
с `dev_uid=202`. Займите места в обеих вкладках и начните игру от первого
игрока. Без явного `dev_uid` вкладка сохраняет свой тестовый ID в sessionStorage.
В production `WEBAPP_DEV` должен быть выключен: параметр `dev_uid` сам по
себе не разрешает вход без подписи Telegram.

## Продакшн (на VPS вместе с ботом)
1. Включи в окружении:
   ```bash
   export WEBAPP_ENABLED=1
   export WEBAPP_PORT=8080
   export WEBAPP_DEV=0
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
python test_holdem.py                # правила, all-in, блайнды, дисквалификация
python -m unittest webapp.test_regressions -v
python -m unittest test_bot_poker -v   # обработчики бота без Telegram и БД
node --check webapp/static/app.js
```

## Ограничения каркаса (осознанные)
- Комнаты живут в памяти процесса (перезапуск — столы сбрасываются). Для
  постоянных столов подойдёт вынос состояния в Redis/БД.
- Обычные комнаты самостоятельны. Комнаты `chat_<id>` используют живой стол
  группового `/holdem`; таймерами и переходами между раздачами управляет бот.
  Они не показываются в публичном списке: вход по ссылке из группы с проверкой членства.
- Самостоятельные веб-столы имеют серверный таймер 120 секунд: сначала авто-чек
  или пас, после двух пропусков — дисквалификация. Следующая раздача запускается
  через 4 секунды. Запуск и закрытие стола доступны только организатору.
- Чужие карты открываются только при вскрытии. Победа после паса соперников
  не раскрывает карты победителя. Аватары берутся из подписанных данных Telegram;
  при отсутствии фотографии показываются инициалы.
