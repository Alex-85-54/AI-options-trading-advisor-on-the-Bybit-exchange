#!/bin/bash
# Скрипт для проверки переменных окружения

echo "=== Проверка .env файла ==="
if [ -f .env ]; then
    echo "✓ .env файл найден"
    echo ""
    echo "Содержимое строки DEEPSEEK_API_KEY:"
    grep "^DEEPSEEK_API_KEY" .env || echo "❌ Строка DEEPSEEK_API_KEY не найдена в .env"
    echo ""
    echo "Все переменные в .env:"
    grep -E "^[A-Z_]+=" .env | sed 's/=.*/=***/' 
else
    echo "❌ .env файл не найден"
fi

echo ""
echo "=== Проверка переменных окружения в системе ==="
if [ -n "$DEEPSEEK_API_KEY" ]; then
    echo "✓ DEEPSEEK_API_KEY установлена (длина: ${#DEEPSEEK_API_KEY})"
else
    echo "❌ DEEPSEEK_API_KEY не установлена"
fi

echo ""
echo "=== Проверка docker-compose конфигурации ==="
if command -v docker-compose &> /dev/null; then
    echo "Проверка переменных в docker-compose:"
    docker-compose config 2>/dev/null | grep -A 5 "DEEPSEEK_API_KEY" || echo "DEEPSEEK_API_KEY не найдена в конфигурации"
elif command -v docker &> /dev/null && docker compose version &> /dev/null; then
    echo "Проверка переменных в docker compose:"
    docker compose config 2>/dev/null | grep -A 5 "DEEPSEEK_API_KEY" || echo "DEEPSEEK_API_KEY не найдена в конфигурации"
else
    echo "❌ Docker Compose не найден"
fi
