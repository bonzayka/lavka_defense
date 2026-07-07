# Python 3.12 — под него у onnxruntime/opencv/numpy есть готовые Linux-колёса,
# поэтому ничего не компилируется (в отличие от 3.14 на хосте).
FROM python:3.12-slim

# Системные библиотеки, которые иногда нужны opencv/onnxruntime в рантайме.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кешируется отдельным слоем).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Гор-детектор (CLIP): CPU-torch + transformers. Тяжёлый слой (~1 ГБ образа),
# ставится ДО копирования кода, чтобы кешировался и не пересобирался при правках.
# CPU-индекс ОБЯЗАТЕЛЕН — иначе pip потянет огромный CUDA-torch (~2.5 ГБ).
# Не нужен гор? Закомментируй эти два RUN (бот тихо работает без гор-слоя).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir "transformers>=4.40"
# Прогрев: качаем CLIP в образ, чтобы старт контейнера не висел на ~600 МБ.
RUN python -c "from transformers import CLIPModel, CLIPProcessor; \
    CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); \
    CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"

# Затем код и эталоны.
COPY . .

# Телеграм-бот на long polling — порт не нужен.
CMD ["python", "bot.py"]
