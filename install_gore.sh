#!/usr/bin/env bash
# Установка ИИ-стека для детектора шок-контента/гора (CLIP) на VPS.
# Запускать из папки бота:  bash install_gore.sh
set -e

PY=./venv/bin/python
PIP=./venv/bin/pip

echo ">>> 1/4 Убираю возможный CUDA-torch (на VPS без GPU он не нужен, ест 2.5 ГБ)…"
$PIP uninstall -y torch 2>/dev/null || true

echo ">>> 2/4 Ставлю CPU-версию torch…"
$PIP install torch --index-url https://download.pytorch.org/whl/cpu

echo ">>> 3/4 Ставлю transformers…"
$PIP install "transformers>=4.40"

echo ">>> 4/4 Проверяю импорт и заранее скачиваю модель CLIP…"
$PY - <<'PYEOF'
import torch, transformers
print("torch:", torch.__version__, "| transformers:", transformers.__version__)
from transformers import CLIPModel, CLIPProcessor
m = "openai/clip-vit-base-patch32"
print("Скачиваю/проверяю модель", m, "…")
CLIPModel.from_pretrained(m); CLIPProcessor.from_pretrained(m)
print("OK: модель готова, кэш в ~/.cache/huggingface/")
PYEOF

echo ""
echo ">>> ГОТОВО. Дальше — ОДНО из двух:"
echo "    • в чате команда:  /reloadgore   (без перезапуска бота), либо"
echo "    • sudo systemctl restart defense-bot"
echo "    Проверка:  /diag  ->  должно быть 'Гор (CLIP): загружен'"
echo ""
echo ">>> Проверь ОЗУ:  free -h   (torch+CLIP ~1 ГБ поверх NudeNet ~400 МБ)"
