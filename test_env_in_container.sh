#!/bin/bash
# Скрипт для проверки переменных окружения внутри контейнера

echo "=== Проверка переменных окружения в контейнере telegram-bot ==="
echo ""
echo "Проверка через docker compose exec:"
docker compose exec telegram-bot env | grep DEEPSEEK || echo "❌ DEEPSEEK_API_KEY не найдена в контейнере"
echo ""
echo "Проверка через docker compose exec python:"
docker compose exec telegram-bot python3 -c "import os; print('DEEPSEEK_API_KEY =', os.getenv('DEEPSEEK_API_KEY', 'NOT_SET'))"
echo ""
echo "Проверка через docker compose exec python config:"
docker compose exec telegram-bot python3 -c "import sys; sys.path.insert(0, '/app'); from config import AGENT_CONFIG; print('AGENT_CONFIG[deepseek_api_key] =', AGENT_CONFIG.get('deepseek_api_key', 'NOT_SET'))"
