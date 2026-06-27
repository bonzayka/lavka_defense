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

# Затем код и эталоны.
COPY . .

# Телеграм-бот на long polling — порт не нужен.
CMD ["python", "bot.py"]
