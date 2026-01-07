FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Устанавливаем curl и uv
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

# Копируем только файлы проекта для установки зависимостей
COPY pyproject.toml uv.lock ./

# Синхронизируем зависимости проекта через uv
RUN uv sync --frozen --no-dev

# Копируем остальной код бота
COPY . .

# По умолчанию запускаем Telegram-бота через uv
CMD ["uv", "run", "telegram_bot.py"]

