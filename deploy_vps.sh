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
