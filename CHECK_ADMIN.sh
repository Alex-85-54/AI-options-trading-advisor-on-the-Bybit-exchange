#!/bin/bash
# Скрипт для проверки доступности админ-панели

echo "=== Проверка админ-панели ==="
echo ""

# Проверка 1: Существует ли файл admin.html
echo "1. Проверка файла admin.html..."
if [ -f "static/admin.html" ]; then
    echo "   ✓ Файл static/admin.html существует"
else
    echo "   ✗ Файл static/admin.html НЕ найден!"
fi
echo ""

# Проверка 2: Проверка контейнеров
echo "2. Проверка Docker контейнеров..."
if command -v docker &> /dev/null; then
    if docker ps --filter "name=option-api-server" --format "{{.Names}}" | grep -q "option-api-server"; then
        echo "   ✓ Контейнер option-api-server запущен"
        echo "   Порт: $(docker ps --filter 'name=option-api-server' --format '{{.Ports}}')"
    else
        echo "   ✗ Контейнер option-api-server НЕ запущен!"
        echo "   Попробуйте запустить: docker compose up -d api-server"
    fi
else
    echo "   ⚠ Docker не установлен или недоступен"
fi
echo ""

# Проверка 3: Проверка порта
echo "3. Проверка порта 8000..."
if command -v curl &> /dev/null; then
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null | grep -q "200"; then
        echo "   ✓ Сервер отвечает на порту 8000"
        echo "   Ответ: $(curl -s http://localhost:8000/health)"
    else
        echo "   ✗ Сервер НЕ отвечает на порту 8000"
        echo "   Проверьте логи: docker logs option-api-server"
    fi
else
    if command -v wget &> /dev/null; then
        if wget -q -O - http://localhost:8000/health 2>/dev/null | grep -q "ok"; then
            echo "   ✓ Сервер отвечает на порту 8000"
        else
            echo "   ✗ Сервер НЕ отвечает на порту 8000"
        fi
    else
        echo "   ⚠ curl и wget недоступны, не могу проверить порт"
    fi
fi
echo ""

# Проверка 4: Проверка админ-панели
echo "4. Проверка админ-панели..."
if command -v curl &> /dev/null; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/admin 2>/dev/null)
    if [ "$HTTP_CODE" = "200" ]; then
        echo "   ✓ Админ-панель доступна: http://localhost:8000/admin"
        echo "   HTTP код: $HTTP_CODE"
    else
        echo "   ✗ Админ-панель недоступна (HTTP код: $HTTP_CODE)"
    fi
else
    echo "   ⚠ curl недоступен, не могу проверить админ-панель"
fi
echo ""

echo "=== Резюме ==="
echo "Для доступа к админ-панели откройте в браузере:"
echo "  http://localhost:8000/admin"
echo ""
echo "Если сервер не отвечает, попробуйте:"
echo "  1. docker compose up -d api-server"
echo "  2. docker logs option-api-server"
echo "  3. Проверьте, не занят ли порт 8000: sudo lsof -i :8000"
