# Базовый образ: официальный Python 3.12
FROM python:3.12-slim

# Чтоб логи сразу шли в stdout/stderr, а не буферились
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Установим системные зависимостями для сборки numpy/pandas (нужно!)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория внутри контейнера
WORKDIR /app

# Сначала копируем requirements, чтобы слои кэша работали нормально
COPY requirements.txt /app/requirements.txt

# Обновим pip и поставим зависимости
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Теперь копируем весь проект
COPY . /app

# Команда запуска бота
CMD ["python", "src/bot.py"]
