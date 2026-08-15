#!/usr/bin/env bash
# Установка бота на чистый Ubuntu-VPS. Запускать ИЗ папки с ботом:
#   bash deploy_vps.sh
set -e

echo ">>> Ставлю системные пакеты (python, venv, либы для opencv/onnxruntime)..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip libgl1 libglib2.0-0

echo ">>> Создаю venv и ставлю зависимости (это тяжёлый ИИ-стек, пару минут)..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo ""
echo ">>> Готово."
echo "    Проверь токен/прокси в config.py, затем запусти:"
echo "        ./venv/bin/python bot.py"
echo "    Для автозапуска как сервис — см. README (раздел про systemd)."
echo ""
echo ">>> (Опционально) Веб-покер — Telegram Mini App (папка webapp/):"
echo "    1) Включи в окружении:  export WEBAPP_ENABLED=1"
echo "       и публичный HTTPS-адрес: export WEBAPP_URL=\"https://poker.ТВОЙ_ДОМЕН\""
echo "    2) Сервер поднимется вместе с ботом на порту WEBAPP_PORT (по умолчанию 8080),"
echo "       по HTTP. Наружу отдавай его ТОЛЬКО через reverse-proxy с TLS, например"
echo "       (caddy):   poker.ТВОЙ_ДОМЕН {  reverse_proxy 127.0.0.1:8080  }"
echo "    3) Тот же https-URL пропиши боту у @BotFather (/setmenubutton или /setdomain)."
echo "    4) В Telegram: напиши боту в ЛС /pokerapp — откроется кнопка веб-стола."
echo "    Локальная отладка в браузере без Telegram:  WEBAPP_DEV=1 ./venv/bin/uvicorn webapp.server:app --port 8080"
