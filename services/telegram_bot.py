from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
import logging
from typing import Dict, List, Tuple, Optional
import requests
import json
from datetime import datetime, timedelta
import asyncio
import re

import sys
from pathlib import Path

# Добавляем корень проекта в путь для импортов
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config import CONFIG, SUBSCRIPTION_CONFIG, AGENT_CONFIG, format_datetime_local
from services.websocket_manager import ws_manager
from services.data_store import data_store
from core.data.option_board import get_option_board
from core.data.database import get_database
from core.agent.decision_engine import get_decision_engine
from core.agent.trading_agent import get_trading_agent
from utils.logging_config import setup_service_logging

# Настройка логирования с ротацией файлов
logger = setup_service_logging(service_name="telegram_bot", log_level=logging.DEBUG)

# Проверка загрузки DEEPSEEK_API_KEY при старте (после настройки logger)
import os
_deepseek_key_from_env = os.getenv("DEEPSEEK_API_KEY", "").strip().strip('"').strip("'")
_deepseek_key_from_config = AGENT_CONFIG.get("deepseek_api_key", "").strip().strip('"').strip("'")

# Используем print для гарантированного вывода, так как logger может не работать на этом этапе
if _deepseek_key_from_env:
    msg = f"✓ DEEPSEEK_API_KEY загружена из env (длина: {len(_deepseek_key_from_env)}, начинается с: {_deepseek_key_from_env[:7]}...)"
    print(msg)
    logger.info(msg)
elif _deepseek_key_from_config:
    msg = f"✓ DEEPSEEK_API_KEY загружена из config (длина: {len(_deepseek_key_from_config)}, начинается с: {_deepseek_key_from_config[:7]}...)"
    print(msg)
    logger.info(msg)
else:
    msg = (
        f"⚠️ DEEPSEEK_API_KEY не найдена!\n"
        f"  os.getenv('DEEPSEEK_API_KEY') = '{os.getenv('DEEPSEEK_API_KEY', 'NOT_SET')}'\n"
        f"  AGENT_CONFIG['deepseek_api_key'] = '{_deepseek_key_from_config}'\n"
        f"  Все env vars с DEEPSEEK: {[k for k in os.environ.keys() if 'DEEPSEEK' in k.upper()]}"
    )
    print(msg)
    logger.warning(msg)

# Конфигурация
# URL сервиса мониторинга берём из переменной окружения (для Docker),
# локально по умолчанию используется localhost.
import os
MONITORING_SERVICE_URL = os.getenv("MONITORING_SERVICE_URL", "http://localhost:8001")
THRESHOLD = 0.01  # Порог равенства цен (1%)
CHECK_INTERVAL = 5  # Интервал проверки в секундах

# Состояния для ConversationHandler
(
    CHOOSING_ACTION,
    CHOOSING_UNDERLYING,
    ENTERING_DAY,
    CHOOSING_MONTH,
    ENTERING_STRIKE,
    CHOOSING_TYPE,
    REMOVING_OPTION,
    WAITING_FOR_DATA,
    CHOOSING_LEVEL_TYPE,
    ENTERING_LEVEL_PRICE,
    REMOVING_LEVEL
) = range(11)

# Константы
CANCEL_TEXT = "❌ Отмена"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UNDERLYING_ASSETS = ["BTC"]


def escape_html(text: str) -> str:
    """
    Экранировать специальные символы HTML для безопасного использования в Telegram
    
    Args:
        text: Текст для экранирования
        
    Returns:
        Экранированный текст
    """
    if not text:
        return ""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


class TelegramOptionBot:
    def __init__(self, token: str):
        self.token = token
        self.user_options: Dict[int, List[Dict]] = {}
        self.user_monitoring: Dict[int, bool] = {}
        self.user_jobs: Dict[int, JobQueue] = {}
        self.pair_status: Dict[int, Dict[Tuple[str, str], bool]] = {}
        self.option_board = get_option_board()
        
        # Состояние агента
        self.agent_enabled: Dict[int, bool] = {}  # Для каждого пользователя отдельно
        self.agent_decision_engine = get_decision_engine(data_store=data_store)
        self.agent_last_run: Dict[int, Optional[datetime]] = {}
        self.agent_last_signal: Dict[int, Optional[Dict]] = {}
        self.db = get_database()

    def _get_user_info(self, update: Update):
        """Универсальный метод для получения информации о пользователе из update"""
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            return user_id, chat_id, message_id, query
        elif hasattr(update, 'message'):
            user_id = update.effective_user.id
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            return user_id, chat_id, message_id, None
        else:
            # Если это CallbackQuery напрямую
            user_id = update.from_user.id
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            return user_id, chat_id, message_id, update

    def _get_main_menu_keyboard(self):
        """
        Создать клавиатуру главного меню с группировкой кнопок
        
        Группы:
        - 📊 Аналитика: Агент, Уровни S/R
        - 💼 Управление позицией: Добавить опцион, Удалить опцион, Статус мониторинга, 
          Запустить мониторинг, Остановить мониторинг, Текущие цены, Активные сигналы
        """
        # Группа "Аналитика" (синяя группа - используем эмодзи для визуального выделения)
        analytics_group = [
            [
                InlineKeyboardButton("🤖 Агент", callback_data="agent_status"),
                InlineKeyboardButton("📊 Уровни S/R", callback_data="set_levels")
            ]
        ]
        
        # Группа "Управление позицией" (зеленая группа)
        position_management_group = [
            [
                InlineKeyboardButton("➕ Добавить опцион", callback_data="add_option"),
                InlineKeyboardButton("🗑️ Удалить опцион", callback_data="remove_option")
            ],
            [
                InlineKeyboardButton("📊 Статус мониторинга", callback_data="monitoring_status"),
                InlineKeyboardButton("📈 Текущие цены", callback_data="current_prices")
            ],
            [
                InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="start_monitoring"),
                InlineKeyboardButton("⏹️ Остановить мониторинг", callback_data="stop_monitoring")
            ],
            [
                InlineKeyboardButton("🚨 Активные сигналы", callback_data="active_signals")
            ]
        ]
        
        # Объединяем группы и добавляем кнопку помощи
        return analytics_group + position_management_group + [
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start - главное меню"""
        user = update.effective_user

        keyboard = self._get_main_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Добавляем заголовки групп в текст сообщения для визуального разделения
        message_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отслеживания опционов Bybit.\n"
            "Отслеживаю равенство цен Call/Put для Стренгла.\n\n"
            "📊 <b>Аналитика</b>\n"
            "Агент | Уровни S/R\n\n"
            "💼 <b>Управление позицией</b>\n"
            "Добавить/Удалить опцион | Мониторинг | Цены | Сигналы"
        )

        await update.message.reply_text(
            message_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return CHOOSING_ACTION

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback от inline кнопок"""
        query = update.callback_query
        await query.answer()

        action = query.data
        
        # Игнорируем кнопки агента - они обрабатываются отдельным обработчиком
        if action in ["agent_toggle", "agent_run_now"]:
            logger.debug(f"handle_callback: игнорируем {action} (обрабатывается _handle_agent_callback)")
            return CHOOSING_ACTION
        
        logger.debug(f"handle_callback: обработка {action}")

        if action == "add_option":
            return await self.start_add_option(update, context)
        elif action == "noop":
            # Заголовки групп (no operation) - просто игнорируем
            await query.answer("")
            return CHOOSING_ACTION
        elif action == "remove_option":
            return await self.start_remove_option(update, context)
        elif action == "monitoring_status":
            return await self.show_monitoring_status(update, context)
        elif action == "start_monitoring":
            return await self.start_monitoring_callback(update, context)
        elif action == "stop_monitoring":
            return await self.stop_monitoring_callback(update, context)
        elif action == "current_prices":
            return await self.show_current_prices(update, context)
        elif action == "active_signals":  # Новый обработчик
            return await self.show_active_signals(update, context)
        elif action == "agent_status":
            return await self.agent_status(update, context)
        elif action == "set_levels":
            return await self.start_set_levels(update, context)
        elif action == "add_level":
            return await self.start_add_level(update, context)
        elif action == "view_levels":
            return await self.view_levels(update, context)
        elif action == "remove_level":
            return await self.start_remove_level(update, context)
        elif action.startswith("level_underlying_"):
            return await self.handle_level_underlying_selection(update, context)
        elif action.startswith("level_type_"):
            return await self.handle_level_type_selection(update, context)
        elif action.startswith("remove_level_"):
            return await self.handle_level_removal_selection(update, context)
        elif action == "help":
            return await self.show_help(update, context)
        elif action == "cancel":
            return await self.cancel_operation(update, context)
        elif action == "back_to_menu":
            return await self.back_to_main_menu(update, context)
        elif action.startswith("underlying_"):
            return await self.handle_underlying_selection(update, context)
        elif action.startswith("month_"):
            return await self.handle_month_selection(update, context)
        elif action.startswith("type_"):
            return await self.handle_type_selection(update, context)
        elif action.startswith("remove_"):
            return await self.handle_removal_selection(update, context)

        return CHOOSING_ACTION

    # ===== ГЛАВНОЕ МЕНЮ И ОСНОВНЫЕ КОМАНДЫ =====

    async def back_to_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Вернуться в главное меню"""
        # Определяем тип update
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            await query.answer()
            chat_id = query.message.chat_id
            user = query.from_user
            is_query = True
        else:
            query = None
            chat_id = update.message.chat_id
            user = update.effective_user
            is_query = False

        keyboard = self._get_main_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Добавляем заголовки групп в текст сообщения для визуального разделения
        message_text = (
            f"👋 <b>Главное меню</b>\n\n"
            f"Пользователь: {user.first_name}\n\n"
            "📊 <b>Аналитика</b>\n"
            "Агент | Уровни S/R\n\n"
            "💼 <b>Управление позицией</b>\n"
            "Добавить/Удалить опцион | Мониторинг | Цены | Сигналы"
        )

        if is_query:
            await query.edit_message_text(
                message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )

        return ConversationHandler.END

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать справку"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        # Используем HTML форматирование для надежности (Markdown может вызывать ошибки парсинга)
        help_text = (
            "📚 <b>Помощь</b>\n\n"
            "<b>Команды:</b>\n"
            "/start - Главное меню\n"
            "/add_option - Добавить опцион\n"
            "/remove_option - Удалить опцион\n"
            "/start_monitoring - Запустить мониторинг\n"
            "/stop_monitoring - Остановить мониторинг\n"
            "/monitoring_status - Статус\n"
            "/current_prices - Текущие цены\n"
            "/agent_status - Статус агента\n"
            "/agent_start - Запустить агента\n"
            "/agent_stop - Остановить агента\n"
            "/set_levels - Управление уровнями S/R\n\n"
            "<b>Как работает:</b>\n"
            "1. Добавьте Call и Put опционы\n"
            "2. Запустите мониторинг\n"
            "3. Бот уведомит, когда цены сравняются\n\n"
            "<b>Порог:</b> 1%\n"
            "<b>Интервал:</b> 5 секунд"
        )

        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                help_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=help_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )

        return CHOOSING_ACTION

    # ===== ДОБАВЛЕНИЕ ОПЦИОНА =====

    async def start_add_option(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать процесс добавления опциона"""
        # Получаем query из update
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            chat_id = query.message.chat_id
            message_id = query.message.message_id
        else:
            query = None
            chat_id = update.message.chat_id
            message_id = None

        keyboard = []
        for asset in UNDERLYING_ASSETS:
            keyboard.append([InlineKeyboardButton(asset, callback_data=f"underlying_{asset}")])
        keyboard.append([InlineKeyboardButton(CANCEL_TEXT, callback_data="cancel")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = "📊 *Добавление опциона*\n\nВыберите базовый актив:"

        if query:
            await query.edit_message_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return CHOOSING_UNDERLYING

    async def handle_underlying_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора базового актива"""
        # Это всегда будет callback_query
        query = update.callback_query
        await query.answer()

        underlying = query.data.split("_")[1]
        context.user_data['underlying'] = underlying

        await query.edit_message_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{underlying}*\n\n"
            f"Введите число месяца экспирации (от 1 до 31):\n"
            f"*Пример:* 4, 15, 25",
            parse_mode='Markdown'
        )
        return ENTERING_DAY

    async def handle_day_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода дня"""
        text = update.message.text.strip()

        # Проверяем формат - просто число без ведущих нулей
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Неверный формат. Введите число от 1 до 31:\n"
                "*Пример:* 4, 15, 25",
                parse_mode='Markdown'
            )
            return ENTERING_DAY

        day_num = int(text)
        if day_num < 1 or day_num > 31:
            await update.message.reply_text(
                "❌ Число должно быть от 1 до 31:\n"
                "*Пример:* 4, 15, 25",
                parse_mode='Markdown'
            )
            return ENTERING_DAY

        # Сохраняем как строку без ведущих нулей
        context.user_data['day'] = str(day_num)

        # Создаем клавиатуру с месяцами
        keyboard = []
        row = []
        for i, month in enumerate(MONTHS, 1):
            row.append(InlineKeyboardButton(month, callback_data=f"month_{month}"))
            if i % 3 == 0:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(CANCEL_TEXT, callback_data="cancel")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{context.user_data['underlying']}*\n"
            f"День: *{context.user_data['day']}*\n\n"
            f"Выберите месяц экспирации:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return CHOOSING_MONTH

    async def handle_month_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора месяца"""
        query = update.callback_query
        await query.answer()

        month = query.data.split("_")[1]
        context.user_data['month'] = month

        await query.edit_message_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{context.user_data['underlying']}*\n"
            f"Экспирация: *{context.user_data['day']}{month}*\n\n"
            f"Введите страйк-цену:\n"
            f"*Пример:* 45000, 1000.5, 0.55",
            parse_mode='Markdown'
        )
        return ENTERING_STRIKE

    async def handle_strike_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода страйка"""
        text = update.message.text.strip()

        # Проверяем, что это число
        try:
            strike = float(text)
            if strike <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Введите положительное число:\n"
                "*Пример:* 45000, 1000.5, 0.55",
                parse_mode='Markdown'
            )
            return ENTERING_STRIKE

        # Сохраняем как строку, убираем лишние нули
        context.user_data['strike'] = str(int(strike)) if strike.is_integer() else str(strike)

        keyboard = [
            [
                InlineKeyboardButton("📈 Call (C)", callback_data="type_C"),
                InlineKeyboardButton("📉 Put (P)", callback_data="type_P")
            ],
            [InlineKeyboardButton(CANCEL_TEXT, callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{context.user_data['underlying']}*\n"
            f"Экспирация: *{context.user_data['day']}{context.user_data['month']}*\n"
            f"Страйк: *{context.user_data['strike']}*\n\n"
            f"Выберите тип опциона:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return CHOOSING_TYPE

    def _get_user_info(self, update: Update):
        """Универсальный метод для получения информации о пользователе из update"""
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            return user_id, chat_id, message_id, query
        elif hasattr(update, 'message'):
            user_id = update.effective_user.id
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            return user_id, chat_id, message_id, None
        else:
            # Если это CallbackQuery напрямую
            user_id = update.from_user.id
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            return user_id, chat_id, message_id, update

    async def handle_type_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора типа опциона"""
        query = update.callback_query
        await query.answer()

        option_type = query.data.split("_")[1]

        # Проверяем, есть ли все необходимые данные
        required_keys = ['underlying', 'day', 'month', 'strike']
        missing_keys = [key for key in required_keys if key not in context.user_data]

        if missing_keys:
            await query.edit_message_text(
                f"❌ Ошибка: отсутствуют данные: {', '.join(missing_keys)}"
            )
            return ConversationHandler.END

        # Создаем символ опциона через менеджер WebSocket
        try:
            symbol = ws_manager.create_option_symbol(
                context.user_data['underlying'],
                context.user_data['day'],
                context.user_data['month'],
                context.user_data['strike'],
                option_type
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Ошибка создания символа: {str(e)}"
            )
            return ConversationHandler.END

        # Сохраняем у пользователя
        user_id = query.from_user.id
        if user_id not in self.user_options:
            self.user_options[user_id] = []

        # Проверяем, нет ли уже такого опциона
        existing_symbols = [opt['symbol'] for opt in self.user_options.get(user_id, [])]
        if symbol in existing_symbols:
            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"⚠️ Опцион *{symbol}* уже есть в вашем списке!",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return ConversationHandler.END

        # Добавляем опцион
        new_option = {
            'symbol': symbol,
            'underlying': context.user_data['underlying'],
            'day': context.user_data['day'],
            'month': context.user_data['month'],
            'strike': context.user_data['strike'],
            'type': option_type,
            'added_at': datetime.now().isoformat()
        }

        self.user_options[user_id].append(new_option)



        # Подписываемся на обновления
        try:
            ws_manager.connect([symbol])
        except Exception as e:
            print(f"ws_manager type: {type(ws_manager)}")
            print(f"ws_manager methods: {dir(ws_manager)}")
            logger.error(f"Error connecting to WebSocket: {e}")
            # Но продолжаем, так как опцион все равно добавлен

        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ *Опцион добавлен!*\n\n"
            f"*Символ:* `{symbol}`\n"
            f"*Тип:* {'📈 Call' if option_type == 'C' else '📉 Put'}\n"
            f"*Страйк:* {context.user_data['strike']}\n"
            f"*Экспирация:* {context.user_data['day']}{context.user_data['month']}\n\n"
            f"Всего опционов: *{len(self.user_options[user_id])}*",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

        # Очищаем временные данные
        context.user_data.clear()

        return ConversationHandler.END

    # ===== ПОКАЗ МОИХ ОПЦИОНОВ =====

    # ===== УДАЛЕНИЕ ОПЦИОНА =====

    async def start_remove_option(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать процесс удаления опциона"""
        # Получаем информацию о пользователе
        user_id, chat_id, message_id, query = self._get_user_info(update)

        options = self.user_options.get(user_id, [])

        if not options:
            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = "🗑️ *Удаление опциона*\n\nУ вас нет опционов для удаления."

            if query:
                await query.edit_message_text(
                    message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            return CHOOSING_ACTION

        # Создаем клавиатуру с опционами
        keyboard = []
        for i, opt in enumerate(options, 1):
            display_text = f"{i}. {opt['symbol']}"
            if len(display_text) > 64:  # Ограничение Telegram
                display_text = f"{i}. {opt['symbol'][:60]}..."
            keyboard.append([InlineKeyboardButton(display_text, callback_data=f"remove_{opt['symbol']}")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = "🗑️ *Удаление опциона*\n\nВыберите опцион для удаления:"

        if query:
            await query.edit_message_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return REMOVING_OPTION

    async def handle_removal_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора опциона для удаления"""
        # Получаем callback_query из update
        query = update.callback_query
        await query.answer()

        symbol_to_remove = query.data.split("_", 1)[1]
        user_id = query.from_user.id

        # Удаляем из списка пользователя
        if user_id in self.user_options:
            initial_count = len(self.user_options[user_id])
            self.user_options[user_id] = [
                opt for opt in self.user_options[user_id]
                if opt['symbol'] != symbol_to_remove
            ]

            # Если список пуст, удаляем запись пользователя
            if not self.user_options[user_id]:
                del self.user_options[user_id]
                remaining_count = 0
            else:
                remaining_count = len(self.user_options[user_id])

            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"✅ *Опцион удален!*\n\n"
                f"Удален: `{symbol_to_remove}`\n"
                f"Было: {initial_count} опционов\n"
                f"Стало: {remaining_count} опционов",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return ConversationHandler.END

    # ===== МОНИТОРИНГ ЦЕН =====

    async def _monitor_prices_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Фоновая задача для мониторинга цен с уведомлениями"""
        job = context.job
        user_id = job.user_id
        chat_id = job.chat_id

        # Проверяем, что мониторинг активен
        if not self.user_monitoring.get(user_id, False):
            logger.debug(f"Monitoring not active for user {user_id}")
            return

        options = self.user_options.get(user_id, [])
        if not options:
            logger.debug(f"No options for user {user_id}")
            return

        # Инициализируем состояние пар для пользователя, если еще нет
        if user_id not in self.pair_status:
            self.pair_status[user_id] = {}

        # Получаем Call и Put опционы
        call_options = [opt for opt in options if opt['type'] == 'C']
        put_options = [opt for opt in options if opt['type'] == 'P']

        if not call_options or not put_options:
            return

        # Простая проверка для отладки
        logger.debug(f"Checking {len(call_options) * len(put_options)} pairs for user {user_id}")

        # Проверяем первую пару для отладки
        if call_options and put_options:
            call_opt = call_options[0]
            put_opt = put_options[0]

            call_data = data_store.get(call_opt['symbol'])
            put_data = data_store.get(put_opt['symbol'])

            if call_data and put_data:
                call_price = call_data.get('ask_price', 0)
                put_price = put_data.get('ask_price', 0)

                price_diff = abs(call_price - put_price)
                avg_price = (call_price + put_price) / 2

                if avg_price > 0:
                    relative_diff = (price_diff / avg_price) * 100
                    logger.debug(
                        f"Sample pair: {call_opt['symbol']} ({call_price}) / {put_opt['symbol']} ({put_price}) - diff: {relative_diff:.2f}%")

                    pair_key = (call_opt['symbol'], put_opt['symbol'])

                    # Определяем текущее состояние пары
                    current_status = relative_diff < (THRESHOLD * 100)

                    # Получаем предыдущее состояние
                    previous_status = self.pair_status[user_id].get(pair_key)

                    # Если состояние изменилось или еще не установлено
                    if previous_status is None or previous_status != current_status:
                        # Обновляем состояние
                        self.pair_status[user_id][pair_key] = current_status

                        logger.info(f"Status changed for {call_opt['symbol']}/{put_opt['symbol']}: "
                                    f"{previous_status} -> {current_status} (diff: {relative_diff:.2f}%)")

                        # Отправляем уведомление
                        await self._send_pair_status_notification(
                            context, chat_id,
                            call_opt['symbol'], put_opt['symbol'],
                            call_price, put_price,
                            price_diff, relative_diff,
                            current_status
                        )


    async def start_monitoring_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запуск мониторинга через callback"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        options = self.user_options.get(user_id, [])

        if not options:
            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = "❌ *Запуск мониторинга*\n\nУ вас нет опционов для отслеживания."

            if query:
                await query.edit_message_text(
                    message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            return CHOOSING_ACTION

        # Запускаем мониторинг
        self.user_monitoring[user_id] = True

        # Инициализируем состояние пар
        if user_id not in self.pair_status:
            self.pair_status[user_id] = {}

            # Добавляем задачу в JobQueue
            # JobQueue доступна через context.job_queue
        if context.job_queue:
            # Удаляем старые задачи
            current_jobs = context.job_queue.get_jobs_by_name(f"monitor_{user_id}")
            for job in current_jobs:
                job.schedule_removal()

            # Добавляем новую задачу
            job = context.job_queue.run_repeating(
                self._monitor_prices_job,
                interval=CHECK_INTERVAL,
                first=1,
                user_id=user_id,
                chat_id=chat_id,
                name=f"monitor_{user_id}"
            )

            if job:
                logger.info(f"✅ Started monitoring job for user {user_id}")
            else:
                logger.error(f"❌ Failed to start monitoring job for user {user_id}")
        else:
            logger.error("❌ JobQueue not available in context")

        # Формируем сообщение об успешном запуске
        call_count = len([opt for opt in options if opt['type'] == 'C'])
        put_count = len([opt for opt in options if opt['type'] == 'P'])

        message = (
            f"✅ *Мониторинг запущен!*\n\n"
            f"*Статус:* 🟢 Активен\n"
            f"*Опционов:* {len(options)} ({call_count}📈 Call, {put_count}📉 Put)\n"
            f"*Порог срабатывания:* {THRESHOLD * 100:.1f}%\n"
            f"*Интервал проверки:* {CHECK_INTERVAL} сек\n\n"
            f"Бот будет присылать уведомления при изменении состояния пар.\n\n"
            f"Для проверки статуса используйте кнопку 📊 Статус мониторинга"
        )

        keyboard = [
            [InlineKeyboardButton("📊 Статус мониторинга", callback_data="monitoring_status")],
            [InlineKeyboardButton("🚨 Активные сигналы", callback_data="active_signals")],
            [InlineKeyboardButton("⏹️ Остановить мониторинг", callback_data="stop_monitoring")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return CHOOSING_ACTION

    async def show_active_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать активные сигналы"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        # Проверяем, запущен ли мониторинг
        if not self.user_monitoring.get(user_id, False):
            message = "🚨 *Активные сигналы*\n\nМониторинг не запущен. Используйте '▶️ Запустить мониторинг'."
        elif user_id not in self.pair_status or not self.pair_status[user_id]:
            message = "🚨 *Активные сигналы*\n\nПока нет отслеживаемых пар. Мониторинг работает, но состояния пар еще не определены."
        else:
            # Считаем активные сигналы (где цены равны)
            active_pairs = []
            for (call_symbol, put_symbol), status in self.pair_status[user_id].items():
                if status:  # True = цены равны
                    active_pairs.append((call_symbol, put_symbol))

            if not active_pairs:
                message = "🚨 *Активные сигналы*\n\nНет активных сигналов. Все цены разошлись."
            else:
                message = f"🚨 *Активные сигналы*\n\nАктивных сигналов: *{len(active_pairs)}*\n\n"

                for i, (call_symbol, put_symbol) in enumerate(active_pairs[:5], 1):
                    # Получаем текущие данные
                    call_data = data_store.get(call_symbol)
                    put_data = data_store.get(put_symbol)

                    if call_data and put_data:
                        call_price = call_data.get('ask_price', 0)
                        put_price = put_data.get('ask_price', 0)
                        price_diff = abs(call_price - put_price)
                        avg_price = (call_price + put_price) / 2
                        relative_diff = (price_diff / avg_price * 100) if avg_price > 0 else 0

                        message += (
                            f"*{i}. Пара #{i}*\n"
                            f"📈 Call: `{call_symbol}`\n"
                            f"Цена: *{call_price:.2f}*\n"
                            f"📉 Put: `{put_symbol}`\n"
                            f"Цена: *{put_price:.2f}*\n"
                            f"Разница: {price_diff:.4f} ({relative_diff:.2f}%)\n"
                            f"Статус: 🟢 *АКТИВЕН*\n\n"
                        )
                    else:
                        message += f"*{i}. {call_symbol} / {put_symbol}*\nДанные не получены\n\n"

                if len(active_pairs) > 5:
                    message += f"*... и еще {len(active_pairs) - 5} активных пар*"

                message += f"\nВсего отслеживаемых пар: *{len(self.pair_status[user_id])}*"

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="active_signals")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return CHOOSING_ACTION

    async def _send_pair_status_notification(self, context, chat_id,
                                             call_symbol, put_symbol,
                                             call_price, put_price,
                                             price_diff, relative_diff,
                                             is_equal):
        """Отправить уведомление об изменении состояния пары"""

        logger.info(f"Preparing notification: {call_symbol} ({call_price}) / {put_symbol} ({put_price}) - "
               f"diff: {price_diff:.4f} ({relative_diff:.2f}%), is_equal: {is_equal}")

        # Используем функцию форматирования с учетом часового пояса
        timestamp = format_datetime_local(datetime.now())

        if is_equal:
            # Цены сравнялись - сигнал к покупке
            message = (
                f"🚨 *СИГНАЛ ДЛЯ ВХОДА!*\n\n"
                f"*Цены опционов сравнялись!*\n\n"
                f"📈 *Call опцион:*\n"
                f"`{call_symbol}`\n"
                f"Цена: *{call_price:.2f}*\n\n"
                f"📉 *Put опцион:*\n"
                f"`{put_symbol}`\n"
                f"Цена: *{put_price:.2f}*\n\n"
                f"*Разница цен:* {price_diff:.4f}\n"
                f"*Процентная разница:* {relative_diff:.2f}%\n"
                f"*Время:* {timestamp}\n\n"
                f"✅ *Сигнал к покупке опционов!*"
            )
        else:
            # Цены разошлись - сброс сигнала
            message = (
                f"ℹ️ *СБРОС СИГНАЛА*\n\n"
                f"*Цены опционов разошлись*\n\n"
                f"📈 *Call опцион:*\n"
                f"`{call_symbol}`\n"
                f"Цена: *{call_price:.2f}*\n\n"
                f"📉 *Put опцион:*\n"
                f"`{put_symbol}`\n"
                f"Цена: *{put_price:.2f}*\n\n"
                f"*Разница цен:* {price_diff:.4f}\n"
                f"*Процентная разница:* {relative_diff:.2f}%\n"
                f"*Время:* {timestamp}\n\n"
                f"⏸️ *Сигнал сброшен*"
            )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                disable_notification=not is_equal  # Вибрация только для сигналов покупки
            )
            logger.info(f"✅ Sent notification for {call_symbol}/{put_symbol}: is_equal={is_equal}")
        except Exception as e:
            logger.error(f"❌ Error sending notification: {e}")

    async def stop_monitoring_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановка мониторинга через callback"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        if self.user_monitoring.get(user_id, False):
            self.user_monitoring[user_id] = False

            # Очищаем состояние пар
            if user_id in self.pair_status:
                del self.pair_status[user_id]

                # Удаляем задачу из JobQueue
            if context.job_queue:
                current_jobs = context.job_queue.get_jobs_by_name(f"monitor_{user_id}")
                for job in current_jobs:
                    job.schedule_removal()
                    logger.info(f"Removed monitoring job for user {user_id}")
            else:
                logger.error("JobQueue not available in context")

            message = "⏹️ *Мониторинг остановлен*"
        else:
            message = "ℹ️ Мониторинг не был запущен."


        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return CHOOSING_ACTION

    async def show_monitoring_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать статус мониторинга с разницей цен"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        options = self.user_options.get(user_id, [])

        if not options:
            keyboard = [
                [InlineKeyboardButton("➕ Добавить опцион", callback_data="add_option")],
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = "📊 *Статус мониторинга*\n\nУ вас нет опционов для отслеживания."

            if query:
                await query.edit_message_text(
                    message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            return CHOOSING_ACTION

        # Получаем Call и Put опционы
        call_options = [opt for opt in options if opt['type'] == 'C']
        put_options = [opt for opt in options if opt['type'] == 'P']

        if not call_options or not put_options:
            message = (
                "📊 *Статус мониторинга*\n\n"
                "❌ Нужны как Call, так и Put опционы.\n"
                f"Сейчас у вас: {len(call_options)} Call, {len(put_options)} Put"
            )
        else:
            # Рассчитываем разницы цен
            price_differences = []

            for call_opt in call_options:
                for put_opt in put_options:
                    call_data = data_store.get(call_opt['symbol'])
                    put_data = data_store.get(put_opt['symbol'])

                    if call_data and put_data:
                        call_price = call_data.get('ask_price', 0)
                        put_price = put_data.get('ask_price', 0)

                        if call_price > 0 and put_price > 0:
                            price_diff = abs(call_price - put_price)
                            avg_price = (call_price + put_price) / 2

                            if avg_price > 0:
                                percent_diff = (price_diff / avg_price) * 100
                                price_differences.append({
                                    'call_symbol': call_opt['symbol'],
                                    'put_symbol': put_opt['symbol'],
                                    'call_price': call_price,
                                    'put_price': put_price,
                                    'price_diff': price_diff,
                                    'percent_diff': percent_diff
                                })

            # Формируем статистику
            if price_differences:
                min_diff = min([d['percent_diff'] for d in price_differences])
                max_diff = max([d['percent_diff'] for d in price_differences])
                avg_diff = sum([d['percent_diff'] for d in price_differences]) / len(price_differences)

                # Находим пару с минимальной разницей
                best_pair = min(price_differences, key=lambda x: x['percent_diff'])

                status_emoji = "🟢" if self.user_monitoring.get(user_id, False) else "🔴"

                message = (
                    f"📊 *Статус мониторинга*\n\n"
                    f"*Статус:* {status_emoji} {'Активен' if self.user_monitoring.get(user_id, False) else 'Не активен'}\n"
                    f"*Опционов:* {len(options)} ({len(call_options)}📈 Call, {len(put_options)}📉 Put)\n\n"
                    f"*Статистика по разнице цен:*\n"
                    f"• Минимальная: {min_diff:.2f}%\n"
                    f"• Максимальная: {max_diff:.2f}%\n"
                    f"• Средняя: {avg_diff:.2f}%\n"
                    f"• Порог срабатывания: {THRESHOLD * 100:.1f}%\n\n"
                    f"*Лучшая пара (наименьшая разница):*\n"
                    f"📈 `{best_pair['call_symbol']}`: {best_pair['call_price']:.2f}\n"
                    f"📉 `{best_pair['put_symbol']}`: {best_pair['put_price']:.2f}\n"
                    f"Разница: {best_pair['price_diff']:.4f} ({best_pair['percent_diff']:.2f}%)"
                )
            else:
                message = (
                    f"📊 *Статус мониторинга*\n\n"
                    f"*Статус:* {'🟢 Активен' if self.user_monitoring.get(user_id, False) else '🔴 Не активен'}\n"
                    f"*Опционов:* {len(options)} ({len(call_options)} Call, {len(put_options)} Put)\n\n"
                    "⏳ Ожидание данных по ценам..."
                )

        keyboard = []
        if self.user_monitoring.get(user_id, False):
            keyboard.append([InlineKeyboardButton("⏹️ Остановить мониторинг", callback_data="stop_monitoring")])
        else:
            keyboard.append([InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="start_monitoring")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        return CHOOSING_ACTION

    async def show_current_prices(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать текущие цены опционов"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        options = self.user_options.get(user_id, [])

        if not options:
            keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = "📈 *Текущие цены*\n\nУ вас нет опционов для отображения."

            if query:
                await query.edit_message_text(
                    message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            return CHOOSING_ACTION

        # Группируем по типу и получаем цены
        call_prices = []
        put_prices = []

        for opt in options:
            data = data_store.get(opt['symbol'])
            if data and 'ask_price' in data:
                price = data['ask_price']
                if opt['type'] == 'C':
                    call_prices.append(f"• `{opt['symbol']}`: {price:.2f}")
                else:
                    put_prices.append(f"• `{opt['symbol']}`: {price:.2f}")
            else:
                if opt['type'] == 'C':
                    call_prices.append(f"• `{opt['symbol']}`: ожидание...")
                else:
                    put_prices.append(f"• `{opt['symbol']}`: ожидание...")

        # Используем функцию форматирования с учетом часового пояса
        current_time = format_datetime_local(datetime.now(), format_str='%H:%M:%S')
        message = (
            f"📈 *Текущие цены*\n\n"
            f"Всего опционов: {len(options)}\n"
            f"Время: {current_time}\n\n"
        )

        if call_prices:
            message += f"*📈 Call опционы:*\n" + "\n".join(call_prices[:5]) + "\n\n"

        if put_prices:
            message += f"*📉 Put опционы:*\n" + "\n".join(put_prices[:5])

        if len(call_prices) > 5 or len(put_prices) > 5:
            message += f"\n\n... и еще опционов"

        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return CHOOSING_ACTION

    # ===== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ =====

    async def cancel_operation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена текущей операции"""
        # Определяем тип update
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            await query.answer()
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            is_query = True
        else:
            query = None
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            is_query = False

        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if is_query:
            await query.edit_message_text(
                "❌ Операция отменена.",
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Операция отменена.",
                reply_markup=reply_markup
            )

        return ConversationHandler.END

    async def agent_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать статус агента"""
        # Обрабатываем как callback query, так и обычное сообщение
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            is_query = True
        else:
            query = None
            user_id = update.effective_user.id
            chat_id = update.message.chat_id
            is_query = False
        
        is_enabled = self.agent_enabled.get(user_id, False)
        last_run = self.agent_last_run.get(user_id)
        last_signal = self.agent_last_signal.get(user_id)
        
        status_emoji = "🟢" if is_enabled else "🔴"
        status_text = "активен" if is_enabled else "остановлен"
        
        message = f"🤖 *Статус торгового агента*\n\n"
        message += f"Статус: {status_emoji} {status_text}\n"
        
        # Используем функцию форматирования с учетом часового пояса
        message += f"Последний запуск: {format_datetime_local(last_run)}\n"
        
        if last_signal:
            signal_type = last_signal.get('signal_type', 'unknown')
            confidence = last_signal.get('confidence', 0)
            signal_timestamp = last_signal.get('timestamp', 'N/A')
            
            # Пытаемся распарсить timestamp, если это строка ISO format
            if isinstance(signal_timestamp, str) and signal_timestamp != 'N/A':
                try:
                    signal_dt = datetime.fromisoformat(signal_timestamp.replace('Z', '+00:00'))
                    signal_timestamp = format_datetime_local(signal_dt)
                except (ValueError, AttributeError):
                    # Если не удалось распарсить, используем как есть
                    pass
            
            message += f"\n📊 Последний сигнал:\n"
            message += f"Тип: {signal_type}\n"
            message += f"Уверенность: {confidence:.0%}\n"
            message += f"Время: {signal_timestamp}\n"
        else:
            message += f"\n📊 Последний сигнал: нет\n"
        
        message += f"\n⚙️ Настройки:\n"
        message += f"Интервал запуска: {AGENT_CONFIG.get('run_interval_minutes', 60)} мин\n"
        message += f"Мин. уверенность: {AGENT_CONFIG.get('min_confidence', 0.6):.0%}\n"
        message += f"Макс. экспирация: {AGENT_CONFIG.get('max_expiration_days', 3)} дней\n"
        
        keyboard = [
            [
                InlineKeyboardButton("▶️ Запустить" if not is_enabled else "⏸ Остановить", 
                                   callback_data="agent_toggle"),
                InlineKeyboardButton("🔄 Запустить сейчас", callback_data="agent_run_now")
            ],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if is_query:
            await query.edit_message_text(
                message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def agent_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запустить агента"""
        user_id = update.effective_user.id
        chat_id = update.message.chat_id
        
        if self.agent_enabled.get(user_id, False):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Агент уже запущен. Используйте /agent_status для просмотра статуса."
            )
            return
        
        self.agent_enabled[user_id] = True
        
        # Запускаем периодический запуск агента
        interval_minutes = AGENT_CONFIG.get("run_interval_minutes", 60)
        
        if context.job_queue:
            # Удаляем старые задачи, если есть
            current_jobs = context.job_queue.get_jobs_by_name(f"agent_{user_id}")
            for job in current_jobs:
                job.schedule_removal()
            
            # Добавляем новую задачу
            job = context.job_queue.run_repeating(
                self._run_agent_periodic,
                interval=interval_minutes * 60,  # в секундах
                first=10,  # Первый запуск через 10 секунд
                name=f"agent_{user_id}",
                data={'user_id': user_id, 'chat_id': chat_id}
            )
            logger.info(f"✅ Агент запущен для пользователя {user_id}, интервал: {interval_minutes} мин")
        else:
            logger.error("JobQueue not available in context")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ *Агент запущен*\n\n"
                 f"Агент будет анализировать рынок каждые {interval_minutes} минут.\n"
                 f"Сигналы будут отправляться автоматически.",
            parse_mode='Markdown'
        )
    
    async def agent_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановить агента"""
        user_id = update.effective_user.id
        chat_id = update.message.chat_id
        
        if not self.agent_enabled.get(user_id, False):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Агент уже остановлен."
            )
            return
        
        self.agent_enabled[user_id] = False
        
        # Удаляем задачи из JobQueue
        if context.job_queue:
            current_jobs = context.job_queue.get_jobs_by_name(f"agent_{user_id}")
            for job in current_jobs:
                job.schedule_removal()
            logger.info(f"⏸ Агент остановлен для пользователя {user_id}")
        else:
            logger.error("JobQueue not available in context")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏸ *Агент остановлен*\n\n"
                 "Периодический анализ рынка прекращен.",
            parse_mode='Markdown'
        )
    
    async def _run_agent_periodic(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодический запуск агента"""
        user_id = context.job.data.get('user_id')
        chat_id = context.job.data.get('chat_id')
        
        if not self.agent_enabled.get(user_id, False):
            # Агент был остановлен, удаляем задачу
            context.job.schedule_removal()
            return
        
        # Запускаем анализ для всех активов
        for underlying in UNDERLYING_ASSETS:
            try:
                logger.info(f"🤖 Запуск анализа агента для {underlying} (пользователь {user_id})")
                decision = self.agent_decision_engine.make_decision(underlying)
                
                self.agent_last_run[user_id] = datetime.now()
                
                if decision:
                    self.agent_last_signal[user_id] = decision
                    await self._send_agent_signal(chat_id, decision, context)
                else:
                    logger.info(f"Агент не нашел подходящих условий для {underlying}")
                    
            except Exception as e:
                logger.error(f"Ошибка при работе агента для {underlying}: {e}", exc_info=True)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка при анализе {underlying}: {str(e)}"
                )
    
    async def _send_agent_signal(self, chat_id: int, signal: Dict, context: ContextTypes.DEFAULT_TYPE):
        """Отправить сигнал от агента в Telegram"""
        try:
            message = self._format_agent_signal(signal)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='HTML'
            )
            
            logger.info(f"✅ Сигнал от агента отправлен в чат {chat_id}")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке сигнала: {e}", exc_info=True)
            # Пытаемся отправить без форматирования в случае ошибки
            try:
                fallback_message = (
                    f"📊 Торговый сигнал от агента\n\n"
                    f"Тип: {signal.get('signal_type', 'unknown')}\n"
                    f"Актив: {signal.get('underlying', 'BTC')}\n"
                    f"Уверенность: {signal.get('confidence', 0):.0%}\n"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=fallback_message,
                    parse_mode=None
                )
            except Exception as e2:
                logger.error(f"Ошибка при отправке fallback сообщения: {e2}", exc_info=True)
    
    def _format_agent_signal(self, signal: Dict) -> str:
        """Форматировать сигнал от агента для отправки в Telegram (HTML форматирование)"""
        signal_type = signal.get('signal_type', 'unknown')
        underlying = signal.get('underlying', 'BTC')
        confidence = signal.get('confidence', 0)
        risk_level = signal.get('risk_level', 'medium')
        reasoning = signal.get('reasoning', '')
        
        # Экранируем пользовательские данные
        signal_type_escaped = escape_html(str(signal_type))
        underlying_escaped = escape_html(str(underlying))
        risk_level_escaped = escape_html(str(risk_level))
        reasoning_escaped = escape_html(str(reasoning)) if reasoning else ''
        
        # Эмодзи для типов сигналов
        type_emojis = {
            'strangle': '📊',
            'straddle': '⚖️',
            'call': '📈',
            'put': '📉'
        }
        type_emoji = type_emojis.get(signal_type, '📊')
        
        # Эмодзи для уровня риска
        risk_emojis = {
            'low': '🟢',
            'medium': '🟡',
            'high': '🔴'
        }
        risk_emoji = risk_emojis.get(risk_level, '🟡')
        
        # Эмодзи для уверенности
        if confidence >= 0.8:
            conf_emoji = '🟢'
        elif confidence >= 0.6:
            conf_emoji = '🟡'
        else:
            conf_emoji = '🟠'
        
        message = f"{type_emoji} <b>Торговый сигнал от агента</b>\n\n"
        message += f"<b>Тип позиции:</b> {signal_type_escaped.upper()}\n"
        message += f"<b>Базовый актив:</b> {underlying_escaped}\n"
        
        # Детали опционов
        if signal_type in ['strangle', 'straddle']:
            strike_call = signal.get('strike_call')
            strike_put = signal.get('strike_put')
            if strike_call and strike_put:
                message += f"<b>Страйк Call:</b> {strike_call:,.0f}\n"
                message += f"<b>Страйк Put:</b> {strike_put:,.0f}\n"
        elif signal_type in ['call', 'put']:
            strike = signal.get('strike')
            if strike:
                message += f"<b>Страйк:</b> {strike:,.0f}\n"
        
        expiration = signal.get('expiration')
        if expiration:
            expiration_escaped = escape_html(str(expiration))
            message += f"<b>Экспирация:</b> {expiration_escaped}\n"
        
        message += f"\n<b>Уверенность:</b> {conf_emoji} {confidence:.0%}\n"
        message += f"<b>Уровень риска:</b> {risk_emoji} {risk_level_escaped}\n"
        
        if reasoning_escaped:
            # Разбиваем reasoning на строки для лучшей читаемости
            reasoning_lines = reasoning_escaped.split('\n')
            message += f"\n<b>Обоснование:</b>\n"
            for line in reasoning_lines[:10]:  # Ограничиваем количество строк
                message += f"{line}\n"
            if len(reasoning_lines) > 10:
                message += f"... (еще {len(reasoning_lines) - 10} строк)\n"
        
        timestamp = signal.get('timestamp')
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                # Используем функцию форматирования с учетом часового пояса
                time_str = format_datetime_local(dt)
                time_escaped = escape_html(str(time_str))
                message += f"\n<b>Время:</b> {time_escaped}\n"
            except:
                timestamp_escaped = escape_html(str(timestamp))
                message += f"\n<b>Время:</b> {timestamp_escaped}\n"
        
        message += f"\n⚠️ <b>Внимание:</b> Это сигнал от ИИ агента. Всегда проверяйте анализ самостоятельно!"
        
        return message
    
    async def _handle_agent_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback для кнопок агента"""
        try:
            query = update.callback_query
            if not query:
                logger.error("_handle_agent_callback вызван без callback_query")
                return
            
            await query.answer()
            
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            callback_data = query.data
            
            logger.info(f"🔔 Обработка callback агента: {callback_data} (пользователь {user_id}, чат {chat_id})")
            print(f"🔔 DEBUG: _handle_agent_callback вызван с data='{callback_data}' для пользователя {user_id}")
            
            if callback_data == "agent_toggle":
                logger.info(f"🔄 Обработка agent_toggle для пользователя {user_id}")
                print(f"🔄 DEBUG: agent_toggle - текущее состояние: enabled={self.agent_enabled.get(user_id, False)}")
                if self.agent_enabled.get(user_id, False):
                    # Останавливаем
                    self.agent_enabled[user_id] = False
                    if context.job_queue:
                        current_jobs = context.job_queue.get_jobs_by_name(f"agent_{user_id}")
                        for job in current_jobs:
                            job.schedule_removal()
                    logger.info(f"⏸ Агент остановлен для пользователя {user_id}")
                    
                    # Обновляем сообщение со статусом
                    await self.agent_status(update, context)
                else:
                    # Запускаем
                    self.agent_enabled[user_id] = True
                    interval_minutes = AGENT_CONFIG.get("run_interval_minutes", 60)
                    if context.job_queue:
                        current_jobs = context.job_queue.get_jobs_by_name(f"agent_{user_id}")
                        for job in current_jobs:
                            job.schedule_removal()
                        job = context.job_queue.run_repeating(
                            self._run_agent_periodic,
                            interval=interval_minutes * 60,
                            first=10,
                            name=f"agent_{user_id}",
                            data={'user_id': user_id, 'chat_id': chat_id}
                        )
                        logger.info(f"✅ Агент запущен для пользователя {user_id}, интервал: {interval_minutes} мин")
                    else:
                        logger.error("JobQueue not available in context")
                    
                    # Обновляем сообщение со статусом
                    await self.agent_status(update, context)
            
            elif callback_data == "agent_run_now":
                # Запускаем анализ немедленно
                logger.info(f"🔄 Запрос на немедленный запуск анализа агента (пользователь {user_id})")
                print(f"🔄 DEBUG: agent_run_now - начинаем анализ для пользователя {user_id}")
                await query.edit_message_text("🔄 Запуск анализа...")
                
                try:
                    # Проверяем, что агент инициализирован
                    if not self.agent_decision_engine:
                        logger.error("DecisionEngine не инициализирован")
                        raise Exception("DecisionEngine не инициализирован")
                    
                    # Проверяем API ключ
                    agent = self.agent_decision_engine.agent
                    if not agent or not agent.api_key:
                        logger.warning("DeepSeek API ключ не установлен")
                        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            "⚠️ *Ошибка конфигурации*\n\n"
                            "DeepSeek API ключ не установлен.\n"
                            "Проверьте переменную окружения DEEPSEEK_API_KEY в файле .env",
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                        return
                    
                    signals_found = []
                    errors = []
                    
                    for underlying in UNDERLYING_ASSETS:
                        try:
                            logger.info(f"🤖 Немедленный запуск анализа агента для {underlying} (пользователь {user_id})")
                            decision = self.agent_decision_engine.make_decision(underlying)
                            self.agent_last_run[user_id] = datetime.now()
                            
                            if decision:
                                self.agent_last_signal[user_id] = decision
                                signals_found.append((underlying, decision))
                                await self._send_agent_signal(chat_id, decision, context)
                                logger.info(f"✅ Сигнал найден для {underlying}")
                            else:
                                logger.info(f"ℹ️ Анализ завершен для {underlying}. Подходящих условий не найдено.")
                        except Exception as e:
                            error_msg = f"Ошибка для {underlying}: {str(e)}"
                            logger.error(f"Ошибка при анализе {underlying}: {e}", exc_info=True)
                            errors.append(error_msg)
                    
                    # Формируем итоговое сообщение
                    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    if signals_found:
                        message = f"✅ *Анализ завершен*\n\n"
                        message += f"Найдено сигналов: {len(signals_found)}\n"
                        for underlying, signal in signals_found:
                            signal_type = signal.get('signal_type', 'unknown')
                            confidence = signal.get('confidence', 0)
                            message += f"\n• {underlying}: {signal_type} (уверенность: {confidence:.0%})"
                        if errors:
                            message += f"\n\n⚠️ Ошибки:\n" + "\n".join(errors)
                    elif errors:
                        message = f"❌ *Ошибки при анализе*\n\n" + "\n".join(errors)
                    else:
                        message = f"ℹ️ *Анализ завершен*\n\n"
                        message += f"Подходящих условий для входа не найдено для всех активов."
                    
                    await query.edit_message_text(
                        message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logger.error(f"Ошибка при немедленном запуске агента: {e}", exc_info=True)
                    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"❌ Ошибка: {str(e)}",
                        reply_markup=reply_markup
                    )
            
            else:
                logger.warning(f"⚠️ Неизвестный callback_data в _handle_agent_callback: {callback_data}")
                await query.edit_message_text(
                    f"⚠️ Неизвестная команда: {callback_data}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")
                    ]])
                )
                
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в _handle_agent_callback: {e}", exc_info=True)
            try:
                if 'query' in locals() and query:
                    await query.answer("❌ Произошла ошибка")
                    await query.edit_message_text(
                        f"❌ Ошибка: {str(e)}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")
                        ]])
                    )
            except Exception as e2:
                logger.error(f"Ошибка при отправке сообщения об ошибке: {e2}", exc_info=True)
    
    # ===== УПРАВЛЕНИЕ УРОВНЯМИ ПОДДЕРЖКИ/СОПРОТИВЛЕНИЯ =====
    
    async def start_set_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать процесс установки уровней поддержки/сопротивления"""
        user_id, chat_id, message_id, query = self._get_user_info(update)
        
        keyboard = [
            [
                InlineKeyboardButton("➕ Добавить уровень", callback_data="add_level"),
                InlineKeyboardButton("📋 Просмотр уровней", callback_data="view_levels")
            ],
            [
                InlineKeyboardButton("🗑️ Удалить уровень", callback_data="remove_level"),
                InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            "📊 *Управление уровнями поддержки/сопротивления*\n\n"
            "Выберите действие:"
        )
        
        if query:
            await query.edit_message_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        
        return CHOOSING_ACTION
    
    async def start_add_level(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать добавление уровня - выбор актива"""
        user_id, chat_id, message_id, query = self._get_user_info(update)
        
        keyboard = []
        for underlying in UNDERLYING_ASSETS:
            keyboard.append([InlineKeyboardButton(
                f"{underlying}",
                callback_data=f"level_underlying_{underlying}"
            )])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = "📊 *Добавление уровня*\n\nВыберите базовый актив:"
        
        if query:
            await query.edit_message_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        
        return CHOOSING_UNDERLYING
    
    async def handle_level_underlying_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора актива для уровня"""
        query = update.callback_query
        await query.answer()
        
        underlying = query.data.replace("level_underlying_", "")
        context.user_data['level_underlying'] = underlying
        
        keyboard = [
            [
                InlineKeyboardButton("🟢 Поддержка", callback_data="level_type_support"),
                InlineKeyboardButton("🔴 Сопротивление", callback_data="level_type_resistance")
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="add_level")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📊 *Добавление уровня для {underlying}*\n\nВыберите тип уровня:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
        return CHOOSING_LEVEL_TYPE
    
    async def handle_level_type_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора типа уровня"""
        query = update.callback_query
        await query.answer()
        
        level_type = query.data.replace("level_type_", "")
        context.user_data['level_type'] = level_type
        
        level_name = "поддержки" if level_type == "support" else "сопротивления"
        underlying = context.user_data.get('level_underlying', 'BTC')
        
        await query.edit_message_text(
            f"📊 *Добавление уровня {level_name} для {underlying}*\n\n"
            f"Введите цену уровня (число):",
            parse_mode='Markdown'
        )
        
        return ENTERING_LEVEL_PRICE
    
    async def handle_level_price_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода цены уровня"""
        user_id, chat_id, message_id, query = self._get_user_info(update)
        
        try:
            price = float(update.message.text.replace(',', '.'))
            
            underlying = context.user_data.get('level_underlying', 'BTC')
            level_type = context.user_data.get('level_type', 'support')
            
            # Сохраняем уровень в БД
            self.db.add_support_resistance_level(underlying, level_type, price)
            
            level_name = "поддержки" if level_type == "support" else "сопротивления"
            emoji = "🟢" if level_type == "support" else "🔴"
            
            keyboard = [
                [InlineKeyboardButton("➕ Добавить еще", callback_data="add_level")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ {emoji} Уровень {level_name} для {underlying} добавлен: *{price:,.2f}*",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
            # Очищаем временные данные
            context.user_data.pop('level_underlying', None)
            context.user_data.pop('level_type', None)
            
            return CHOOSING_ACTION
            
        except ValueError:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Неверный формат. Введите число (например: 89500 или 89500.5)"
            )
            return ENTERING_LEVEL_PRICE
        except Exception as e:
            logger.error(f"Ошибка при добавлении уровня: {e}", exc_info=True)
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка при добавлении уровня: {str(e)}",
                reply_markup=reply_markup
            )
            return CHOOSING_ACTION
    
    async def view_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Просмотр всех уровней"""
        user_id, chat_id, message_id, query = self._get_user_info(update)
        
        try:
            all_levels = self.db.get_all_support_resistance_levels()
            
            if not all_levels:
                message = "📊 *Уровни поддержки/сопротивления*\n\nУровни не установлены."
            else:
                message = "📊 *Уровни поддержки/сопротивления*\n\n"
                
                for underlying in UNDERLYING_ASSETS:
                    if underlying in all_levels:
                        levels = all_levels[underlying]
                        support_levels = levels.get('support', [])
                        resistance_levels = levels.get('resistance', [])
                        
                        message += f"*{underlying}:*\n"
                        
                        if support_levels:
                            support_str = ", ".join([f"{p:,.2f}" for p in support_levels])
                            message += f"🟢 Поддержка: {support_str}\n"
                        
                        if resistance_levels:
                            resistance_str = ", ".join([f"{p:,.2f}" for p in resistance_levels])
                            message += f"🔴 Сопротивление: {resistance_str}\n"
                        
                        message += "\n"
            
            keyboard = [
                [InlineKeyboardButton("➕ Добавить уровень", callback_data="add_level")],
                [InlineKeyboardButton("🗑️ Удалить уровень", callback_data="remove_level")],
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if query:
                await query.edit_message_text(
                    message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            
            return CHOOSING_ACTION
            
        except Exception as e:
            logger.error(f"Ошибка при просмотре уровней: {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка: {str(e)}"
            )
            return ConversationHandler.END
    
    async def start_remove_level(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать удаление уровня"""
        user_id, chat_id, message_id, query = self._get_user_info(update)
        
        try:
            all_levels = self.db.get_all_support_resistance_levels()
            
            if not all_levels:
                keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                message_text = "🗑️ *Удаление уровня*\n\nУровни не установлены."
                
                if query:
                    await query.edit_message_text(
                        message_text,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message_text,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                return CHOOSING_ACTION
            
            # Создаем клавиатуру с уровнями для удаления
            keyboard = []
            for underlying in UNDERLYING_ASSETS:
                if underlying in all_levels:
                    levels = all_levels[underlying]
                    for level_type, prices in levels.items():
                        for price in prices:
                            level_name = "🟢 Поддержка" if level_type == "support" else "🔴 Сопротивление"
                            button_text = f"{underlying} {level_name} {price:,.0f}"
                            if len(button_text) > 64:
                                button_text = f"{underlying} {level_name[:1]} {price:,.0f}"
                            # Используем точку как разделитель вместо подчеркивания для цены
                            price_str = str(price).replace(".", "DOT")  # Заменяем точку на DOT
                            callback_data = f"remove_level_{underlying}_{level_type}_{price_str}"
                            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_text = "🗑️ *Удаление уровня*\n\nВыберите уровень для удаления:"
            
            if query:
                await query.edit_message_text(
                    message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            
            return REMOVING_LEVEL
            
        except Exception as e:
            logger.error(f"Ошибка при начале удаления уровня: {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка: {str(e)}"
            )
            return ConversationHandler.END
    
    async def handle_level_removal_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора уровня для удаления"""
        query = update.callback_query
        await query.answer()
        
        try:
            # Парсим callback_data: remove_level_{underlying}_{level_type}_{price}
            # Формат: remove_level_BTC_support_89500 или remove_level_BTC_support_89500DOT5
            data_str = query.data.replace("remove_level_", "")
            parts = data_str.split("_", 2)  # Разделяем только первые 2 части
            underlying = parts[0]
            level_type = parts[1]
            price_str = parts[2] if len(parts) > 2 else ""
            # Восстанавливаем точку из DOT
            price = float(price_str.replace("DOT", ".").replace(",", "."))
            
            # Удаляем уровень из БД
            success = self.db.remove_support_resistance_level(underlying, level_type, price)
            
            if success:
                level_name = "поддержки" if level_type == "support" else "сопротивления"
                emoji = "🟢" if level_type == "support" else "🔴"
                
                keyboard = [
                    [InlineKeyboardButton("🗑️ Удалить еще", callback_data="remove_level")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"✅ {emoji} Уровень {level_name} для {underlying} удален: *{price:,.2f}*",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "❌ Уровень не найден или уже удален.",
                    reply_markup=reply_markup
                )
            
            return CHOOSING_ACTION
            
        except Exception as e:
            logger.error(f"Ошибка при удалении уровня: {e}", exc_info=True)
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_levels")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"❌ Ошибка: {str(e)}",
                reply_markup=reply_markup
            )
            return CHOOSING_ACTION
    
    def _auto_subscribe_on_startup(self):
        """
        Автоматическая подписка на опционы при старте приложения
        
        Получает список доступных экспираций для базовых активов (BTC, ETH, SOL)
        и подписывается на опционы через WebSocket согласно конфигурации.
        """
        logger.info("🔄 Начинаю автоматическую подписку на опционы при старте...")
        
        max_days = SUBSCRIPTION_CONFIG.get("max_expiration_days", 3)
        underlying_assets = UNDERLYING_ASSETS  # ["BTC", "ETH", "SOL"]
        
        all_symbols = []
        
        for underlying in underlying_assets:
            try:
                logger.info(f"📊 Получение доски опционов для {underlying}...")
                board_data = self.option_board.get_option_board(underlying, max_days=max_days)
                
                symbols = board_data.get('symbols', [])
                expirations = board_data.get('expirations', [])
                underlying_price = board_data.get('underlying_price')
                
                if not symbols:
                    logger.warning(f"⚠️ Не найдено опционов для {underlying}")
                    continue
                
                logger.info(
                    f"✅ Найдено {len(symbols)} опционов для {underlying}: "
                    f"{len(expirations)} экспираций, цена: {underlying_price}"
                )
                
                all_symbols.extend(symbols)
                
            except Exception as e:
                logger.error(f"❌ Ошибка при получении опционов для {underlying}: {e}", exc_info=True)
                continue
        
        if not all_symbols:
            logger.warning("⚠️ Не найдено опционов для автоматической подписки")
            return
        
        # Подписываемся на все найденные опционы
        try:
            logger.info(f"🔌 Подписка на {len(all_symbols)} опционов через WebSocket...")
            ws_manager.connect(all_symbols, wait_for_data=False)
            logger.info(f"✅ Автоматическая подписка завершена: {len(all_symbols)} опционов")
        except Exception as e:
            logger.error(f"❌ Ошибка при подписке на опционы: {e}", exc_info=True)

    def run(self):
        """Запуск бота"""
        application = Application.builder().token(self.token).build()

        # ConversationHandler для добавления опциона
        add_option_conv = ConversationHandler(
            entry_points=[
                CommandHandler("add_option", self.start_add_option),
                CallbackQueryHandler(self.start_add_option, pattern="^add_option$")
            ],
            states={
                CHOOSING_UNDERLYING: [
                    CallbackQueryHandler(self.handle_underlying_selection, pattern="^underlying_"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel$"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ],
                ENTERING_DAY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_day_input)
                ],
                CHOOSING_MONTH: [
                    CallbackQueryHandler(self.handle_month_selection, pattern="^month_"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel$")
                ],
                ENTERING_STRIKE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_strike_input)
                ],
                CHOOSING_TYPE: [
                    CallbackQueryHandler(self.handle_type_selection, pattern="^type_"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel$")
                ]
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel_operation),
                CallbackQueryHandler(self.cancel_operation, pattern="^cancel$"),
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$"),
                # Обработка кнопок агента из любого состояния
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_run_now)$")
            ]
        )
        
        # ConversationHandler для удаления опциона
        remove_option_conv = ConversationHandler(
            entry_points=[
                CommandHandler("remove_option", self.start_remove_option),
                CallbackQueryHandler(self.start_remove_option, pattern="^remove_option$")
            ],
            states={
                REMOVING_OPTION: [
                    CallbackQueryHandler(self.handle_removal_selection, pattern="^remove_"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ]
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel_operation),
                CallbackQueryHandler(self.cancel_operation, pattern="^cancel$"),
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$"),
                # Обработка кнопок агента из любого состояния
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_run_now)$")
            ]
        )
        
        # ConversationHandler для установки уровней поддержки/сопротивления
        set_levels_conv = ConversationHandler(
            entry_points=[
                CommandHandler("set_levels", self.start_set_levels),
                CallbackQueryHandler(self.start_set_levels, pattern="^set_levels$")
            ],
            states={
                CHOOSING_ACTION: [
                    CallbackQueryHandler(self.start_add_level, pattern="^add_level$"),
                    CallbackQueryHandler(self.view_levels, pattern="^view_levels$"),
                    CallbackQueryHandler(self.start_remove_level, pattern="^remove_level$"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ],
                CHOOSING_UNDERLYING: [
                    CallbackQueryHandler(self.handle_level_underlying_selection, pattern="^level_underlying_"),
                    CallbackQueryHandler(self.start_set_levels, pattern="^set_levels$"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ],
                CHOOSING_LEVEL_TYPE: [
                    CallbackQueryHandler(self.handle_level_type_selection, pattern="^level_type_"),
                    CallbackQueryHandler(self.start_add_level, pattern="^add_level$"),
                    CallbackQueryHandler(self.start_set_levels, pattern="^set_levels$"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ],
                ENTERING_LEVEL_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_level_price_input)
                ],
                REMOVING_LEVEL: [
                    CallbackQueryHandler(self.handle_level_removal_selection, pattern="^remove_level_"),
                    CallbackQueryHandler(self.start_set_levels, pattern="^set_levels$"),
                    CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
                ]
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel_operation),
                CallbackQueryHandler(self.cancel_operation, pattern="^cancel$"),
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$"),
                # Обработка кнопок агента из любого состояния
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_run_now)$")
            ]
        )

        # Основной обработчик для главного меню
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.show_help))
        application.add_handler(CommandHandler("monitoring_status", self.show_monitoring_status))
        application.add_handler(CommandHandler("start_monitoring", self.start_monitoring_callback))
        application.add_handler(CommandHandler("stop_monitoring", self.stop_monitoring_callback))
        application.add_handler(CommandHandler("current_prices", self.show_current_prices))
        application.add_handler(CommandHandler("active_signals", self.show_active_signals))
        
        # Команды агента
        application.add_handler(CommandHandler("agent_status", self.agent_status))
        application.add_handler(CommandHandler("agent_start", self.agent_start))
        application.add_handler(CommandHandler("agent_stop", self.agent_stop))
        
        # Команды для уровней поддержки/сопротивления
        application.add_handler(CommandHandler("set_levels", self.start_set_levels))

        # Добавляем ConversationHandler'ы
        application.add_handler(add_option_conv)
        application.add_handler(remove_option_conv)
        application.add_handler(set_levels_conv)

        # ВАЖНО: Специфичные обработчики должны быть ПЕРЕД общим обработчиком
        # Обработчик callback для кнопок агента (должен быть первым, чтобы не перехватывался общим обработчиком)
        agent_callback_handler = CallbackQueryHandler(
            self._handle_agent_callback,
            pattern="^(agent_toggle|agent_run_now)$"
        )
        application.add_handler(agent_callback_handler)
        logger.info("✅ Обработчик кнопок агента зарегистрирован (agent_toggle, agent_run_now)")
        
        # Обработчик callback для кнопок главного меню (общий обработчик - в конце)
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^active_signals$"))
        logger.info("✅ Общий обработчик callback зарегистрирован")


        # Автоматическая подписка на опционы при старте
        self._auto_subscribe_on_startup()
        
        # Запускаем бота
        logger.info("Бот запущен...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )


# Создание и запуск бота
if __name__ == "__main__":
    # Дополнительная проверка DEEPSEEK_API_KEY при запуске
    import os
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip().strip('"').strip("'")
    if deepseek_key:
        print(f"✓ DEEPSEEK_API_KEY найдена при запуске (длина: {len(deepseek_key)}, начинается с: {deepseek_key[:7]}...)")
        logger.info(f"✓ DEEPSEEK_API_KEY найдена при запуске (длина: {len(deepseek_key)})")
    else:
        print(f"⚠️ DEEPSEEK_API_KEY не найдена при запуске!")
        print(f"  os.getenv('DEEPSEEK_API_KEY') = '{os.getenv('DEEPSEEK_API_KEY', 'NOT_SET')}'")
        print(f"  Все env vars с DEEPSEEK: {[k for k in os.environ.keys() if 'DEEPSEEK' in k.upper()]}")
        logger.warning(f"⚠️ DEEPSEEK_API_KEY не найдена при запуске! os.getenv('DEEPSEEK_API_KEY') = '{os.getenv('DEEPSEEK_API_KEY', 'NOT_SET')}'")
    
    bot = TelegramOptionBot(CONFIG["telegram_token"])
    bot.run()