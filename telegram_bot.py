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

from config import CONFIG
from websocket_manager import ws_manager
from data_store import data_store

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # Установите DEBUG для отладки

# Добавьте этот handler если еще нет
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

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
    WAITING_FOR_DATA
) = range(8)

# Константы
CANCEL_TEXT = "❌ Отмена"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UNDERLYING_ASSETS = ["BTC", "ETH", "SOL"]


class TelegramOptionBot:
    def __init__(self, token: str):
        self.token = token
        self.user_options: Dict[int, List[Dict]] = {}
        self.user_monitoring: Dict[int, bool] = {}
        self.user_jobs: Dict[int, JobQueue] = {}
        self.pair_status: Dict[int, Dict[Tuple[str, str], bool]] = {}

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
        """Создать клавиатуру главного меню"""
        return [
            [
                InlineKeyboardButton("➕ Добавить опцион", callback_data="add_option"),
                InlineKeyboardButton("📋 Мои опционы", callback_data="my_options")
            ],
            [
                InlineKeyboardButton("🗑️ Удалить опцион", callback_data="remove_option"),
                InlineKeyboardButton("📊 Статус мониторинга", callback_data="monitoring_status")
            ],
            [
                InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="start_monitoring"),
                InlineKeyboardButton("⏹️ Остановить мониторинг", callback_data="stop_monitoring")
            ],
            [
                InlineKeyboardButton("📈 Текущие цены", callback_data="current_prices"),
                InlineKeyboardButton("🚨 Активные сигналы", callback_data="active_signals")
            ],
            [
                InlineKeyboardButton("❓ Помощь", callback_data="help")
            ]
        ]

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start - главное меню"""
        user = update.effective_user

        keyboard = self._get_main_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отслеживания опционов Bybit.\n"
            "Отслеживаю равенство цен Call/Put для Стренгла.\n\n"
            "Выберите действие:",
            reply_markup=reply_markup
        )
        return CHOOSING_ACTION

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback от inline кнопок"""
        query = update.callback_query
        await query.answer()

        action = query.data

        if action == "add_option":
            return await self.start_add_option(update, context)
        elif action == "my_options":
            return await self.show_my_options(update, context)
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

        message_text = (
            f"👋 Главное меню\n\n"
            f"Пользователь: {user.first_name}"
        )

        if is_query:
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=reply_markup
            )

        return ConversationHandler.END

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать справку"""
        user_id, chat_id, message_id, query = self._get_user_info(update)

        help_text = (
            "📚 *Помощь*\n\n"
            "*Команды:*\n"
            "/start - Главное меню\n"
            "/add_option - Добавить опцион\n"
            "/my_options - Мои опционы\n"
            "/remove_option - Удалить опцион\n"
            "/start_monitoring - Запустить мониторинг\n"
            "/stop_monitoring - Остановить мониторинг\n"
            "/monitoring_status - Статус\n"
            "/current_prices - Текущие цены\n\n"
            "*Как работает:*\n"
            "1. Добавьте Call и Put опционы\n"
            "2. Запустите мониторинг\n"
            "3. Бот уведомит, когда цены сравняются\n\n"
            "*Порог:* 1%\n"
            "*Интервал:* 5 секунд"
        )

        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(
                help_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=help_text,
                parse_mode='Markdown',
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

    async def show_my_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать список опционов пользователя"""
        # Определяем тип update
        if hasattr(update, 'callback_query') and update.callback_query:
            query = update.callback_query
            await query.answer()
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            is_query = True
        elif hasattr(update, 'message'):
            query = None
            user_id = update.effective_user.id
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            is_query = False
        else:
            # Это CallbackQuery напрямую
            query = update
            await query.answer()
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            is_query = True

        options = self.user_options.get(user_id, [])

        if not options:
            keyboard = [
                [InlineKeyboardButton("➕ Добавить опцион", callback_data="add_option")],
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = "📋 *Мои опционы*\n\nУ вас пока нет опционов."

            if is_query and query:
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

        # Группируем по типу
        call_options = [opt for opt in options if opt['type'] == 'C']
        put_options = [opt for opt in options if opt['type'] == 'P']

        # Получаем текущие цены
        price_info = []
        for opt in options:
            data = data_store.get(opt['symbol'])
            if data and 'ask_price' in data:
                price = data['ask_price']
                price_info.append(f"• `{opt['symbol']}` - {price:.2f}")
            else:
                price_info.append(f"• `{opt['symbol']}` - ожидание данных...")

        message = (
                f"📋 *Мои опционы*\n\n"
                f"Всего: *{len(options)}* опционов\n"
                f"📈 Call: *{len(call_options)}*\n"
                f"📉 Put: *{len(put_options)}*\n\n"
                f"*Список опционов:*\n" + "\n".join(price_info[:10])
        )

        if len(options) > 10:
            message += f"\n\n... и еще {len(options) - 10} опционов"

        keyboard = [
            [InlineKeyboardButton("🗑️ Удалить опцион", callback_data="remove_option")],
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

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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

        message = (
            f"📈 *Текущие цены*\n\n"
            f"Всего опционов: {len(options)}\n"
            f"Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
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
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
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
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
            ]
        )

        # Основной обработчик для главного меню
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.show_help))
        application.add_handler(CommandHandler("my_options", self.show_my_options))
        application.add_handler(CommandHandler("monitoring_status", self.show_monitoring_status))
        application.add_handler(CommandHandler("start_monitoring", self.start_monitoring_callback))
        application.add_handler(CommandHandler("stop_monitoring", self.stop_monitoring_callback))
        application.add_handler(CommandHandler("current_prices", self.show_current_prices))
        application.add_handler(CommandHandler("active_signals", self.show_active_signals))

        # Добавляем ConversationHandler'ы
        application.add_handler(add_option_conv)
        application.add_handler(remove_option_conv)

        # Обработчик callback для кнопок главного меню
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^active_signals$"))


        # Запускаем бота
        logger.info("Бот запущен...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )


# Создание и запуск бота
if __name__ == "__main__":
    bot = TelegramOptionBot(CONFIG["telegram_token"])
    bot.run()