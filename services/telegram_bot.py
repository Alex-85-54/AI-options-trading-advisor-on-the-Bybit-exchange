from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, BotCommand
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

from config import (
    CONFIG,
    SUBSCRIPTION_CONFIG,
    AGENT_CONFIG,
    BOT_CONFIG,
    format_datetime_local,
    DISPLAY_TIMEZONE,
)
from services.data_store import RemoteDataStore
from core.data.database import get_database
from core.agent.decision_engine import get_decision_engine
from core.agent.trading_agent import get_trading_agent
from core.strategy.gex_calculator import (
    options_from_datastore_for_gex,
    calculate_gex_by_strike,
    max_abs_gex,
    build_gex_chart_png,
    build_oi_by_strike_chart_png,
    build_iv_chart_png,
    build_oi_chart_png,
    compute_iv_atm_from_board,
    build_volatility_smile_chart_png_three_series,
)
from core.strategy.entry_checklist import run_entry_checklist
from utils.logging_config import setup_service_logging

# Уровень и параметры берутся из LOGGING_CONFIG / переменных окружения
logger = setup_service_logging(service_name="telegram_bot")

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
API_SERVER_URL = os.getenv("API_SERVER_URL", "http://api-server:7000")
LIVE_DATA_MAX_AGE_SECONDS = int(os.getenv("LIVE_DATA_MAX_AGE_SECONDS", "180"))
THRESHOLD = 0.01  # Порог равенства цен (1%)
CHECK_INTERVAL = 5  # Интервал проверки в секундах

# Единый источник live-данных для бота — api-server (там WebSocket).
# Локальная in-memory копия в контейнере бота не используется.
data_store = RemoteDataStore(API_SERVER_URL)

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
    REMOVING_LEVEL,
    GEX_CHOOSING_UNDERLYING,
    GEX_ENTERING_DAY,
    GEX_CHOOSING_MONTH,
    GEX_MONITOR_MENU,
    GEX_MONITOR_ENTER_THRESHOLD,
    GEX_MONITOR_ENTER_INTERVAL,
) = range(17)

# Константы
CANCEL_TEXT = "❌ Отмена"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UNDERLYING_ASSETS = ["BTC"]
# Текст кнопки быстрого доступа в главное меню (Reply-клавиатура и обработка сообщения)
MENU_BUTTON_TEXT = "🏠 Меню"


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
        # Состояние агента
        self.agent_enabled: Dict[int, bool] = {}  # Для каждого пользователя отдельно
        self.agent_decision_engine = get_decision_engine(data_store=data_store)
        self.agent_last_run: Dict[int, Optional[datetime]] = {}
        self.agent_last_signal: Dict[int, Optional[Dict]] = {}
        self.db = get_database()

    def _create_option_symbol(
        self,
        underlying: str,
        day: str,
        month: str,
        strike: str,
        option_type: str,
    ) -> str:
        year = CONFIG["expiration_year"]
        return f"{underlying}-{day}{month}{year}-{strike}-{option_type}-USDT"

    def _subscribe_symbols_via_api(self, symbols: List[str]) -> None:
        """Единый источник WS: подписками управляет только api-server."""
        try:
            requests.post(
                f"{API_SERVER_URL}/subscriptions/update",
                json=symbols,
                timeout=8,
            )
        except Exception as e:
            logger.error("Не удалось обновить подписки через api-server: %s", e)

    def _fetch_live_data(self, underlyings: List[str]) -> Dict[str, Dict]:
        """Получить текущую доску опционов из api-server (а не из локального data_store бота)."""
        merged: Dict[str, Dict] = {}
        for underlying in sorted(set(underlyings)):
            try:
                resp = requests.get(f"{API_SERVER_URL}/data/underlying/{underlying}", timeout=8)
                if resp.status_code != 200:
                    logger.warning("api-server /data/underlying/%s -> %s", underlying, resp.status_code)
                    continue
                payload = resp.json() if resp.content else {}
                opts = payload.get("options") if isinstance(payload, dict) else None
                if isinstance(opts, dict):
                    merged.update(opts)
            except Exception as e:
                logger.error("Ошибка запроса live-данных %s из api-server: %s", underlying, e)
        return merged

    def _latest_timestamp_for_prefix(self, data: Dict[str, Dict], prefix: str) -> Optional[datetime]:
        """Вернуть максимальный timestamp по символам префикса."""
        best: Optional[datetime] = None
        for sym, row in data.items():
            if not sym.startswith(prefix):
                continue
            ts = row.get("timestamp")
            if not ts:
                continue
            dt: Optional[datetime] = None
            if isinstance(ts, datetime):
                dt = ts
            elif isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    dt = None
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=DISPLAY_TIMEZONE)
            if best is None or dt > best:
                best = dt
        return best

    def _is_live_data_fresh(self, data: Dict[str, Dict], underlying: str, exp_str: str) -> bool:
        prefix = f"{underlying}-{exp_str}-"
        ts = self._latest_timestamp_for_prefix(data, prefix)
        if ts is None:
            return False
        age = (datetime.now(DISPLAY_TIMEZONE) - ts.astimezone(DISPLAY_TIMEZONE)).total_seconds()
        return age <= LIVE_DATA_MAX_AGE_SECONDS

    def _live_data_staleness_info(self, data: Dict[str, Dict], underlying: str, exp_str: str) -> str:
        """Текст с фактическим временем последнего обновления и возрастом данных."""
        prefix = f"{underlying}-{exp_str}-"
        ts = self._latest_timestamp_for_prefix(data, prefix)
        if ts is None:
            return "Последний timestamp: нет данных."
        ts_local = ts.astimezone(DISPLAY_TIMEZONE)
        age_sec = int((datetime.now(DISPLAY_TIMEZONE) - ts_local).total_seconds())
        return f"Последний timestamp: {format_datetime_local(ts_local)} (возраст: {age_sec} сек)."

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
        - 📊 Аналитика: Агент, Уровни S/R, Расчёт индикаторов
        - 💼 Мониторинг пар (подменю): Добавить/Удалить опцион, Статус, Цены, Запуск/Остановка, Активные сигналы
        """
        # Группа "Аналитика"
        analytics_group = [
            [
                InlineKeyboardButton("🤖 Агент", callback_data="agent_status"),
                InlineKeyboardButton("📊 Уровни S/R", callback_data="set_levels")
            ],
            [
                InlineKeyboardButton("📈 Расчёт индикаторов", callback_data="gex_menu")
            ]
        ]

        # Кнопка "Мониторинг пар" — ведёт в подменю с управлением парами опционов
        position_management_group = [
            [InlineKeyboardButton("📋 Мониторинг пар", callback_data="pair_monitoring_menu")]
        ]
        return analytics_group + position_management_group + [
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]

    def _get_menu_reply_keyboard(self) -> ReplyKeyboardMarkup:
        """Клавиатура с кнопкой быстрого доступа в главное меню (одно нажатие — сразу меню)."""
        return ReplyKeyboardMarkup(
            [[KeyboardButton(MENU_BUTTON_TEXT)]],
            resize_keyboard=True,
            is_persistent=True,
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start — главное меню и кнопка быстрого доступа."""
        user = update.effective_user
        chat_id = update.message.chat_id
        keyboard = self._get_main_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отслеживания опционов Bybit.\n"
            "Отслеживаю равенство цен Call/Put для Стренгла.\n\n"
            "📊 <b>Аналитика</b>\n"
            "Агент | Уровни S/R | Расчёт индикаторов\n\n"
            "💼 <b>Мониторинг пар</b>\n"
            "Равенство цен Call/Put для входа в Стрэнгл"
        )
        await update.message.reply_text(
            message_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="👇 Нажмите кнопку ниже для быстрого перехода в главное меню",
            reply_markup=self._get_menu_reply_keyboard()
        )
        return CHOOSING_ACTION

    async def show_main_menu_for_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать главное меню в ответ на кнопку «Меню» — без лишнего шага с /start."""
        user = update.effective_user
        keyboard = self._get_main_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = (
            f"👋 <b>Главное меню</b>\n\n"
            f"Пользователь: {user.first_name}\n\n"
            "📊 <b>Аналитика</b>\n"
            "Агент | Уровни S/R | Расчёт индикаторов\n\n"
            "💼 <b>Мониторинг пар</b>\n"
            "Равенство цен Call/Put для входа в Стрэнгл"
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
        if action in (
            "agent_toggle",
            "agent_start_periodic",
            "agent_stop_periodic",
            "agent_run_now",
        ):
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
        elif action == "pair_monitoring_menu":
            return await self.show_pair_monitoring_menu(update, context)
        elif action.startswith("underlying_"):
            return await self.handle_underlying_selection(update, context)
        elif action.startswith("month_"):
            return await self.handle_month_selection(update, context)
        elif action.startswith("type_"):
            return await self.handle_type_selection(update, context)
        elif action.startswith("remove_"):
            return await self.handle_removal_selection(update, context)
        elif action == "gex_menu":
            return await self.gex_show_menu(update, context)
        elif action == "gex_add_preset":
            return await self.gex_start_add_preset(update, context)
        elif action == "gex_list_presets":
            return await self.gex_list_presets(update, context)
        elif action == "gex_calc":
            return await self.gex_calculate_all(update, context)
        elif action == "checklist_run":
            return await self.checklist_run(update, context)
        elif action.startswith("gex_underlying_"):
            return await self.gex_handle_underlying(update, context)
        elif action.startswith("gex_month_"):
            return await self.gex_handle_month(update, context)
        elif action.startswith("gex_del_"):
            return await self.gex_handle_delete_preset(update, context)

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
            "Агент | Уровни S/R | Расчёт индикаторов\n\n"
            "💼 <b>Мониторинг пар</b>\n"
            "Равенство цен Call/Put для входа в Стрэнгл"
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

    def _get_pair_monitoring_menu_keyboard(self):
        """Клавиатура подменю «Мониторинг пар»."""
        return [
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
            [InlineKeyboardButton("🚨 Активные сигналы", callback_data="active_signals")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]
        ]

    async def show_pair_monitoring_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать подменю «Мониторинг пар» (равенство цен для входа в Стрэнгл)."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        keyboard = self._get_pair_monitoring_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "📋 <b>Мониторинг пар</b>\n\n"
            "Управление парами опционов и отслеживание равенства цен Call/Put для входа в Стрэнгл."
        )
        if query:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup)
        return CHOOSING_ACTION

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

    # ===== GEX (Gamma Exposure) =====

    async def gex_show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать меню GEX: пресеты и расчёт."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        keyboard = [
            [InlineKeyboardButton("➕ Добавить экспирацию", callback_data="gex_add_preset")],
            [InlineKeyboardButton("📋 Список экспираций", callback_data="gex_list_presets")],
            [InlineKeyboardButton("📊 Рассчитать индикаторы", callback_data="gex_calc")],
            [InlineKeyboardButton("📉 Мониторинг GEX", callback_data="gex_monitor_menu")],
            [InlineKeyboardButton("✅ Проверка входа по чек-листу", callback_data="checklist_run")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "📈 <b>Расчёт индикаторов</b>\n\n"
            "Экспирация = тикер + дата. По нажатию «Рассчитать индикаторы» строятся графики GEX, IV (ATM) и OI по каждой добавленной экспирации (опционы с DTE ≤ 3). "
            "«Проверка входа по чек-листу» оценивает условия входа по 9 параметрам."
        )
        if query:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup)
        return CHOOSING_ACTION

    async def gex_start_add_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать добавление пресета GEX: выбор тикера."""
        query = update.callback_query
        await query.answer()
        keyboard = []
        for asset in UNDERLYING_ASSETS:
            keyboard.append([InlineKeyboardButton(asset, callback_data=f"gex_underlying_{asset}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📈 <b>Добавление экспирации</b>\n\nВыберите базовый актив (тикер):",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return GEX_CHOOSING_UNDERLYING

    def _gex_day_keyboard(self) -> InlineKeyboardMarkup:
        """Клавиатура выбора дня месяца: плитка 5×6 (1–30), затем 31 и «Назад»."""
        keyboard = []
        for row_start in range(1, 31, 5):  # ряды по 5 кнопок: 1–5, 6–10, …, 26–30
            row = [
                InlineKeyboardButton(str(d), callback_data=f"gex_day_{d}")
                for d in range(row_start, min(row_start + 5, 31))
            ]
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("31", callback_data="gex_day_31")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu")])
        return InlineKeyboardMarkup(keyboard)

    async def gex_handle_underlying(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить тикер пресета и показать выбор дня месяца (кнопки 1–31)."""
        query = update.callback_query
        await query.answer()
        underlying = query.data.replace("gex_underlying_", "")
        context.user_data["gex_underlying"] = underlying
        reply_markup = self._gex_day_keyboard()
        await query.edit_message_text(
            f"📈 Экспирация: <b>{underlying}</b>\n\nВыберите число месяца экспирации:",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return GEX_ENTERING_DAY

    async def gex_handle_day_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора дня месяца по кнопке (1–31) для пресета GEX."""
        query = update.callback_query
        await query.answer()
        try:
            day_num = int(query.data.replace("gex_day_", ""))
        except ValueError:
            return GEX_ENTERING_DAY
        if day_num < 1 or day_num > 31:
            return GEX_ENTERING_DAY
        context.user_data["gex_day"] = day_num
        keyboard = []
        for month in MONTHS:
            keyboard.append([InlineKeyboardButton(month, callback_data=f"gex_month_{month}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"📈 Экспирация: {context.user_data.get('gex_underlying')}, день {day_num}\n\nВыберите месяц экспирации:",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return GEX_CHOOSING_MONTH

    async def gex_handle_month(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить пресет GEX (тикер + дата экспирации) и вернуться в меню GEX."""
        query = update.callback_query
        await query.answer()
        month = query.data.replace("gex_month_", "")
        underlying = context.user_data.get("gex_underlying", "")
        day = context.user_data.get("gex_day", 1)
        year_suffix = CONFIG.get("expiration_year", "26")
        expiration_str = f"{day}{month}{year_suffix}"
        added = self.db.add_gex_preset(query.from_user.id, underlying, expiration_str)
        if added:
            await query.edit_message_text(
                f"✅ Экспирация добавлена: <b>{underlying}</b> {expiration_str}",
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                f"ℹ️ Экспирация уже есть: <b>{underlying}</b> {expiration_str}",
                parse_mode='HTML'
            )
        keyboard = [
            [InlineKeyboardButton("➕ Добавить экспирацию", callback_data="gex_add_preset")],
            [InlineKeyboardButton("📋 Список экспираций", callback_data="gex_list_presets")],
            [InlineKeyboardButton("📊 Рассчитать индикаторы", callback_data="gex_calc")],
            [InlineKeyboardButton("📉 Мониторинг GEX", callback_data="gex_monitor_menu")],
            [InlineKeyboardButton("✅ Проверка входа по чек-листу", callback_data="checklist_run")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")]
        ]
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📈 Расчёт индикаторов",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    async def gex_list_presets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать список пресетов GEX с кнопками удаления."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        presets = self.db.get_gex_presets(user_id)
        if not presets:
            text = "📋 Экспираций пока нет. Добавьте экспирацию (тикер + дата)."
            keyboard = [[InlineKeyboardButton("➕ Добавить экспирацию", callback_data="gex_add_preset")]]
        else:
            text = "📋 <b>Список экспираций</b>\n\nНажмите на экспирацию, чтобы удалить:"
            keyboard = []
            for p in presets:
                label = f"{p['underlying']} {p['expiration_str']}"
                keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"gex_del_{p['id']}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        if query:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup)
        return CHOOSING_ACTION

    async def gex_handle_delete_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Удалить пресет GEX по id."""
        query = update.callback_query
        await query.answer()
        try:
            preset_id = int(query.data.replace("gex_del_", ""))
        except ValueError:
            return await self.gex_list_presets(update, context)
        deleted = self.db.delete_gex_preset(query.from_user.id, preset_id)
        if deleted:
            await query.edit_message_text("✅ Экспирация удалена.")
        else:
            await query.edit_message_text("❌ Не удалось удалить экспирацию.")
        return await self.gex_list_presets(update, context)

    # ===== Мониторинг GEX по порогу =====

    def _gex_monitor_menu_keyboard(self):
        """Клавиатура меню мониторинга GEX."""
        return [
            [InlineKeyboardButton("🔔 Задать порог оповещения", callback_data="gex_monitor_set_threshold")],
            [InlineKeyboardButton("⏱ Задать частоту проверки", callback_data="gex_monitor_set_interval")],
            [InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="gex_monitor_start")],
            [InlineKeyboardButton("⏹ Остановить мониторинг", callback_data="gex_monitor_stop")],
            [InlineKeyboardButton("📊 Статус мониторинга", callback_data="gex_monitor_status")],
            [InlineKeyboardButton("⚙ Параметры мониторинга GEX", callback_data="gex_monitor_params")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="gex_monitor_back")],
        ]

    async def gex_monitor_show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать меню мониторинга GEX (вход в подменю)."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        keyboard = self._gex_monitor_menu_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "📉 <b>Мониторинг GEX</b>\n\n"
            "Отслеживание максимального отклонения GEX по модулю по страйкам. "
            "При превышении порога придёт оповещение, мониторинг остановится автоматически.\n\n"
            "Задайте порог и частоту проверки, затем запустите мониторинг. Используются экспирации из раздела «Расчёт индикаторов»."
        )
        if query:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup)
        return GEX_MONITOR_MENU

    async def gex_monitor_handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка кнопок в меню мониторинга GEX."""
        query = update.callback_query
        await query.answer()
        action = query.data
        if action == "gex_monitor_set_threshold":
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="gex_monitor_menu")]]
            await query.edit_message_text(
                "🔔 <b>Порог оповещения</b>\n\n"
                "Введите положительное число (порог в единицах GEX, до 2 знаков после запятой). "
                "Десятичное число вводите через точку.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return GEX_MONITOR_ENTER_THRESHOLD
        if action == "gex_monitor_set_interval":
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="gex_monitor_menu")]]
            await query.edit_message_text(
                "⏱ <b>Частота проверки</b>\n\nВведите интервал в минутах (целое число > 0):",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return GEX_MONITOR_ENTER_INTERVAL
        if action == "gex_monitor_start":
            return await self._gex_monitor_start(update, context)
        if action == "gex_monitor_stop":
            return await self._gex_monitor_stop(update, context)
        if action == "gex_monitor_status":
            return await self._gex_monitor_status(update, context)
        if action == "gex_monitor_params":
            return await self._gex_monitor_params(update, context)
        if action == "gex_monitor_back":
            await self.gex_show_menu(update, context)
            return ConversationHandler.END
        return GEX_MONITOR_MENU

    async def gex_monitor_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: назад в меню GEX или возврат в подменю мониторинга."""
        query = update.callback_query
        if query:
            await query.answer()
        if query.data == "gex_monitor_back":
            await self.gex_show_menu(update, context)
            return ConversationHandler.END
        if query.data == "gex_monitor_menu":
            return await self.gex_monitor_show_menu(update, context)

    async def gex_monitor_handle_threshold_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода порога мониторинга GEX."""
        text = update.message.text.strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("❌ Введите число (например 50000 или 100000).")
            return GEX_MONITOR_ENTER_THRESHOLD
        if value <= 0:
            await update.message.reply_text("❌ Порог должен быть положительным числом.")
            return GEX_MONITOR_ENTER_THRESHOLD
        user_id = update.effective_user.id
        value = round(value, 2)
        self.db.set_gex_monitor_threshold(user_id, value)
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await update.message.reply_text(
            f"✅ <b>Порог сохранён:</b> {value:,.2f}",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def gex_monitor_handle_interval_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода частоты проверки (мин)."""
        text = update.message.text.strip()
        if not text.isdigit():
            await update.message.reply_text("❌ Введите целое число минут (например 5 или 10).")
            return GEX_MONITOR_ENTER_INTERVAL
        value = int(text)
        if value <= 0:
            await update.message.reply_text("❌ Интервал должен быть больше 0.")
            return GEX_MONITOR_ENTER_INTERVAL
        user_id = update.effective_user.id
        self.db.set_gex_monitor_interval(user_id, value)
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await update.message.reply_text(
            f"✅ <b>Частота проверки сохранена:</b> {value} мин",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def _gex_monitor_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запуск мониторинга GEX: проверка настроек, при необходимости одна проверка, создание job."""
        user_id, chat_id, _, query = self._get_user_info(update)
        settings = self.db.get_gex_monitor_settings(user_id)
        threshold = settings.get("threshold")
        interval_minutes = settings.get("interval_minutes")
        if threshold is None or interval_minutes is None or threshold <= 0 or interval_minutes <= 0:
            keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
            msg = "⚠️ <b>Сначала задайте порог и частоту.</b>\n\nУкажите порог оповещения и интервал проверки в минутах."
            if query:
                await query.edit_message_text(msg, parse_mode='HTML', reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', reply_markup=keyboard)
            return GEX_MONITOR_MENU
        presets = self.db.get_gex_presets(user_id)
        if not presets:
            keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
            msg = "⚠️ Нет экспираций. Добавьте экспирации в разделе «Расчёт индикаторов»."
            if query:
                await query.edit_message_text(msg, parse_mode='HTML', reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', reply_markup=keyboard)
            return GEX_MONITOR_MENU
        underlyings = [p["underlying"] for p in presets]
        all_data = self._fetch_live_data(underlyings)
        exceeded_expirations = []
        for p in presets:
            if not self._is_live_data_fresh(all_data, p["underlying"], p["expiration_str"]):
                continue
            opts = options_from_datastore_for_gex(all_data, p["underlying"], p["expiration_str"])
            if not opts:
                continue
            gex_by_strike = calculate_gex_by_strike(opts)
            max_val, _ = max_abs_gex(gex_by_strike)
            if max_val > threshold:
                exceeded_expirations.append((p["underlying"], p["expiration_str"], max_val))
        if exceeded_expirations:
            for underlying, exp_str, max_val in exceeded_expirations:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Превышен порог по GEX</b>\n\n"
                        f"Экспирация: <b>{underlying} {exp_str}</b>\n"
                        f"Макс. |GEX|: {max_val:,.0f} (порог: {threshold:,.2f})\n\n"
                        f"Мониторинг остановлен автоматически."
                    ),
                    parse_mode='HTML'
                )
            keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
            await query.edit_message_text(
                "⏹ Мониторинг не запущен: порог уже превышен по указанным экспирациям. Задайте больший порог или дождитесь снижения GEX.",
                parse_mode='HTML',
                reply_markup=keyboard
            )
            return GEX_MONITOR_MENU
        if context.job_queue:
            current_jobs = context.job_queue.get_jobs_by_name(f"gex_monitor_{user_id}")
            for job in current_jobs:
                job.schedule_removal()
            context.job_queue.run_repeating(
                self._gex_monitor_job,
                interval=interval_minutes * 60,
                first=1,
                name=f"gex_monitor_{user_id}",
                data={"user_id": user_id, "chat_id": chat_id}
            )
            logger.info(f"Started GEX monitor job for user {user_id}, interval={interval_minutes} min")
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await query.edit_message_text(
            f"✅ <b>Мониторинг GEX запущен</b>\n\nПорог: {threshold:,.2f}\nЧастота: {interval_minutes} мин\nЭкспираций: {len(presets)}",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def _gex_monitor_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановка мониторинга GEX."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if context.job_queue:
            current_jobs = context.job_queue.get_jobs_by_name(f"gex_monitor_{user_id}")
            for job in current_jobs:
                job.schedule_removal()
            if current_jobs:
                logger.info(f"Stopped GEX monitor job for user {user_id}")
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await query.edit_message_text(
            "⏹ <b>Мониторинг GEX остановлен</b>",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def _gex_monitor_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статус мониторинга GEX."""
        user_id, chat_id, _, query = self._get_user_info(update)
        running = False
        if context.job_queue:
            jobs = context.job_queue.get_jobs_by_name(f"gex_monitor_{user_id}")
            running = bool(jobs)
        settings = self.db.get_gex_monitor_settings(user_id)
        threshold = settings.get("threshold")
        interval = settings.get("interval_minutes")
        status_text = "🟢 <b>Активен</b>" if running else "🔴 <b>Остановлен</b>"
        params = f"Порог: {threshold:,.2f}" if threshold is not None else "Порог: не задан"
        params += f"\nЧастота: {interval} мин" if interval is not None else "\nЧастота: не задана"
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await query.edit_message_text(
            f"📊 <b>Статус мониторинга GEX</b>\n\n{status_text}\n\n{params}",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def _gex_monitor_params(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать параметры мониторинга GEX (порог и частота)."""
        user_id, chat_id, _, query = self._get_user_info(update)
        settings = self.db.get_gex_monitor_settings(user_id)
        threshold = settings.get("threshold")
        interval = settings.get("interval_minutes")
        t_str = f"{threshold:,.2f}" if threshold is not None else "не задан"
        i_str = f"{interval} мин" if interval is not None else "не задана"
        keyboard = InlineKeyboardMarkup(self._gex_monitor_menu_keyboard())
        await query.edit_message_text(
            f"⚙ <b>Параметры мониторинга GEX</b>\n\n🔔 Порог оповещения: {t_str}\n⏱ Частота проверки: {i_str}",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return GEX_MONITOR_MENU

    async def _gex_monitor_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодическая проверка GEX: при превышении порога — оповещение и остановка job."""
        job = context.job
        data = job.data if isinstance(job.data, dict) else {}
        user_id = data.get("user_id")
        chat_id = data.get("chat_id")
        if user_id is None or chat_id is None:
            return
        settings = self.db.get_gex_monitor_settings(user_id)
        threshold = settings.get("threshold")
        if threshold is None or threshold <= 0:
            job.schedule_removal()
            return
        presets = self.db.get_gex_presets(user_id)
        if not presets:
            return
        underlyings = [p["underlying"] for p in presets]
        all_data = self._fetch_live_data(underlyings)
        exceeded = []
        for p in presets:
            if not self._is_live_data_fresh(all_data, p["underlying"], p["expiration_str"]):
                continue
            opts = options_from_datastore_for_gex(all_data, p["underlying"], p["expiration_str"])
            if not opts:
                continue
            gex_by_strike = calculate_gex_by_strike(opts)
            max_val, strike_at = max_abs_gex(gex_by_strike)
            if max_val > threshold:
                exceeded.append((p["underlying"], p["expiration_str"], max_val, strike_at))
        if not exceeded:
            return
        for underlying, exp_str, max_val, strike_at in exceeded:
            strike_info = f" (страйк {strike_at:,.0f})" if strike_at is not None else ""
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Превышен порог по GEX</b>\n\n"
                        f"Экспирация: <b>{underlying} {exp_str}</b>\n"
                        f"Макс. |GEX|: {max_val:,.0f}{strike_info}\n"
                        f"Порог: {threshold:,.2f}\n\n"
                        f"Мониторинг остановлен автоматически."
                    ),
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"GEX monitor: failed to send alert: {e}")
        job.schedule_removal()
        logger.info(f"GEX monitor: threshold exceeded for user {user_id}, job removed")

    async def gex_calculate_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Рассчитать GEX по всем пресетам пользователя и отправить графики."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        presets = self.db.get_gex_presets(user_id)
        if not presets:
            if query:
                await query.edit_message_text(
                    "📊 Нет экспираций. Добавьте экспирацию (тикер + дата), затем нажмите «Рассчитать индикаторы»."
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="📊 Нет экспираций. Добавьте экспирацию, затем нажмите «Рассчитать индикаторы»."
                )
            return CHOOSING_ACTION
        underlyings = [p["underlying"] for p in presets]
        all_data = self._fetch_live_data(underlyings)
        if not all_data:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Нет текущих данных с WebSocket (api-server). Проверьте сервис api-server и переподписку.",
            )
            return CHOOSING_ACTION
        sent = 0
        for p in presets:
            underlying = p["underlying"]
            exp_str = p["expiration_str"]
            if not self._is_live_data_fresh(all_data, underlying, exp_str):
                stale_info = self._live_data_staleness_info(all_data, underlying, exp_str)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ {underlying} {exp_str}: текущие данные устарели или отсутствуют "
                        f"(старше {LIVE_DATA_MAX_AGE_SECONDS} сек).\n{stale_info}"
                    ),
                )
                continue
            opts = options_from_datastore_for_gex(all_data, underlying, exp_str)
            if not opts:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {underlying} {exp_str}: нет данных в текущей доске (подпишитесь на опционы или подождите обновления)."
                )
                continue
            gex_by_strike = calculate_gex_by_strike(opts)
            underlying_price = None
            for sym, d in all_data.items():
                if sym.startswith(f"{underlying}-{exp_str}-") and d.get("underlying_price"):
                    underlying_price = d["underlying_price"]
                    break
            levels = self.db.get_support_resistance_levels(underlying)
            title = f"GEX {underlying} {exp_str}"
            png_bytes = build_gex_chart_png(
                gex_by_strike,
                title=title,
                underlying_price=underlying_price,
                support_resistance_levels=levels if (levels.get("support") or levels.get("resistance")) else None,
            )
            await context.bot.send_photo(chat_id=chat_id, photo=png_bytes, caption=title)
            sent += 1
            # OI по страйкам (гистограмма: Calls и Puts на одной диаграмме)
            oi_strike_png = build_oi_by_strike_chart_png(all_data, underlying, exp_str, underlying_price=underlying_price)
            if oi_strike_png:
                await context.bot.send_photo(chat_id=chat_id, photo=oi_strike_png, caption=f"OI по страйкам {underlying} {exp_str}")
                sent += 1
            # Улыбка волатильности (третья по счёту — после OI по страйкам)
            now_local = datetime.now(DISPLAY_TIMEZONE)
            two_hours_ago = (now_local - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            hour_ts_2h = two_hours_ago.strftime("%Y-%m-%d %H:00")
            yesterday_date = (now_local.date() - timedelta(days=1))
            yesterday_20_ts = f"{yesterday_date.isoformat()} 20:00"
            snapshots_3h = self.db.get_option_snapshots_hourly(underlying, exp_str, hours=3)
            snapshot_2h = snapshots_3h.get(hour_ts_2h) if snapshots_3h else None
            exp_date = self.db.parse_expiration_date(exp_str.upper())
            exp_prev = (exp_date - timedelta(days=1)) if exp_date else None
            if exp_prev:
                exp_str_prev = self.db.expiration_date_to_str(exp_prev)
                snapshots_prev = self.db.get_option_snapshots_hourly(underlying, exp_str_prev, hours=48)
                snapshot_yesterday = snapshots_prev.get(yesterday_20_ts) if snapshots_prev else None
            else:
                snapshot_yesterday = None
            smile_png = build_volatility_smile_chart_png_three_series(
                all_data, underlying, exp_str,
                snapshot_2h_ago=snapshot_2h,
                snapshot_yesterday=snapshot_yesterday,
                label_2h="2 ч назад",
                label_yesterday="Вчера 20:00",
            )
            if smile_png:
                await context.bot.send_photo(chat_id=chat_id, photo=smile_png, caption=f"Улыбка волатильности {underlying} {exp_str}")
                sent += 1
            # IV (ATM) по часам из снимков на границе часа; текущее IV_ATM — из текущей доски
            iv_series = self.db.get_iv_atm_hourly(underlying, exp_str, hours=24)
            current_iv = compute_iv_atm_from_board(all_data, underlying, exp_str)
            if iv_series:
                iv_title = f"IV (ATM) {underlying} {exp_str}"
                iv_png = build_iv_chart_png(iv_series, title=iv_title, current_iv=current_iv)
                await context.bot.send_photo(chat_id=chat_id, photo=iv_png, caption=iv_title + (f"  |  Текущее: {current_iv:.2%}" if current_iv is not None else ""))
                sent += 1
            # OI по часам за 8 часов из БД (снимок на границе часа); текущее OI — из текущей доски
            oi_series = self.db.get_oi_hourly(underlying, exp_str, hours=24)
            current_oi = sum(
                (d.get("open_interest") or 0) for sym, d in all_data.items()
                if sym.startswith(f"{underlying}-{exp_str}-")
            )
            if oi_series:
                oi_title = f"OI {underlying} {exp_str}"
                oi_png = build_oi_chart_png(oi_series, title=oi_title, current_oi=current_oi if current_oi else None)
                await context.bot.send_photo(chat_id=chat_id, photo=oi_png, caption=oi_title + (f"  |  Текущее: {current_oi:,.0f}" if current_oi else ""))
                sent += 1
        if query and sent == 0:
            await query.edit_message_text("📊 По экспирациям нет данных для расчёта (нет опционов в доске с DTE ≤ 3).")
        elif sent > 0:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu"),
                    InlineKeyboardButton("🔄 Пересчитать индикаторы", callback_data="gex_calc"),
                ]
            ])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Отправлено графиков: {sent}",
                reply_markup=keyboard
            )
        return CHOOSING_ACTION

    async def checklist_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Проверка входа по чек-листу: 9 параметров, итог 0–3 пропуск, 4–6 половинный размер, 7–9 полный вход."""
        user_id, chat_id, _, query = self._get_user_info(update)
        if query:
            await query.answer()
        presets = self.db.get_gex_presets(user_id)
        if not presets:
            text = "📋 Нет экспираций. Добавьте экспирацию в разделе «Расчёт индикаторов», затем нажмите «Проверка входа по чек-листу»."
            if query:
                await query.edit_message_text(text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            return CHOOSING_ACTION
        underlyings = [p["underlying"] for p in presets]
        all_data = self._fetch_live_data(underlyings)
        if not all_data:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Нет текущих данных с WebSocket (api-server). Проверьте сервис api-server и переподписку.",
            )
            return CHOOSING_ACTION
        sent = 0
        for p in presets:
            underlying = p["underlying"]
            exp_str = p["expiration_str"]
            prefix = f"{underlying}-{exp_str}-"
            if not self._is_live_data_fresh(all_data, underlying, exp_str):
                stale_info = self._live_data_staleness_info(all_data, underlying, exp_str)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ {underlying} {exp_str}: текущие данные устарели или отсутствуют "
                        f"(старше {LIVE_DATA_MAX_AGE_SECONDS} сек).\n{stale_info}"
                    ),
                )
                continue
            if not any(s.startswith(prefix) for s in all_data.keys()):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {underlying} {exp_str}: нет данных в доске (подпишитесь на опционы или подождите обновления)."
                )
                continue
            try:
                results, score, interpretation = run_entry_checklist(
                    underlying, exp_str, all_data, self.db
                )
            except Exception as e:
                logger.exception("Ошибка чек-листа для %s %s", underlying, exp_str)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка расчёта чек-листа для {underlying} {exp_str}: {e}"
                )
                continue
            lines = [f"✅ <b>Чек-лист входа</b> {underlying} {exp_str}\n"]
            for label, passed in results:
                icon = "✅" if passed else "❌"
                lines.append(f"• <b>{label}</b> {icon}")
            lines.append("")
            # Прогресс-бар: заполненные и пустые клетки (вес каждого пункта = 1)
            bar_filled = "█" * score
            bar_empty = "░" * (9 - score)
            lines.append(f"<b>Итог:</b> [{bar_filled}{bar_empty}] <b>{score}/9</b>")
            # Жёсткие условия: п.1 (ATM IV не в пампе) и п.9 (BE реалистичны)
            hard1_ok = results[0][1]
            hard9_ok = results[8][1]
            if not hard1_ok:
                lines.append(f"🚫 <b>Не входить:</b> не выполнено жёсткое условие «{results[0][0]}»")
            elif not hard9_ok:
                lines.append(f"🚫 <b>Не входить:</b> не выполнено жёсткое условие «{results[8][0]}»")
            else:
                lines.append(f"<b>Интерпретация:</b> {interpretation}")
                lines.append("<i>Жёсткие условия 1 и 9 выполнены.</i>")
                if score <= 3:
                    lines.append("<i>(0–3 → пропуск; 4–6 → половинный размер; 7–9 → полный вход)</i>")
            text = "\n".join(lines)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            sent += 1
        if sent > 0:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="gex_menu"),
                    InlineKeyboardButton("🔄 Пересчитать чек-лист", callback_data="checklist_run"),
                ]
            ])
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Чек-лист отправлен по каждой экспирации.",
                reply_markup=keyboard
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

    def _add_option_day_keyboard(self) -> InlineKeyboardMarkup:
        """Клавиатура выбора дня месяца для добавления опциона: плитка 5×6 (1–30), затем 31 и «Назад»."""
        keyboard = []
        for row_start in range(1, 31, 5):
            row = [
                InlineKeyboardButton(str(d), callback_data=f"option_day_{d}")
                for d in range(row_start, min(row_start + 5, 31))
            ]
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("31", callback_data="option_day_31")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="option_back_underlying")])
        return InlineKeyboardMarkup(keyboard)

    async def handle_underlying_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора базового актива — показываем выбор дня месяца кнопками."""
        # Это всегда будет callback_query
        query = update.callback_query
        await query.answer()

        underlying = query.data.split("_")[1]
        context.user_data['underlying'] = underlying

        reply_markup = self._add_option_day_keyboard()
        await query.edit_message_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{underlying}*\n\n"
            f"Выберите число месяца экспирации:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return ENTERING_DAY

    async def handle_day_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора дня месяца по кнопке (1–31) при добавлении опциона."""
        query = update.callback_query
        await query.answer()
        try:
            day_num = int(query.data.replace("option_day_", ""))
        except ValueError:
            return ENTERING_DAY
        if day_num < 1 or day_num > 31:
            return ENTERING_DAY
        context.user_data['day'] = str(day_num)

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

        await query.edit_message_text(
            f"📊 *Добавление опциона*\n\n"
            f"Базовый актив: *{context.user_data['underlying']}*\n"
            f"День: *{context.user_data['day']}*\n\n"
            f"Выберите месяц экспирации:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return CHOOSING_MONTH

    async def handle_back_to_underlying(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Возврат к выбору базового актива при добавлении опциона."""
        query = update.callback_query
        await query.answer()
        keyboard = []
        for asset in UNDERLYING_ASSETS:
            keyboard.append([InlineKeyboardButton(asset, callback_data=f"underlying_{asset}")])
        keyboard.append([InlineKeyboardButton(CANCEL_TEXT, callback_data="cancel")])
        await query.edit_message_text(
            "📊 *Добавление опциона*\n\nВыберите базовый актив:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHOOSING_UNDERLYING

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

        # Создаем символ опциона (формат Bybit)
        try:
            symbol = self._create_option_symbol(
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



        # Подписку обновляет api-server (единый источник WebSocket)
        self._subscribe_symbols_via_api([symbol])

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
        
        if is_enabled:
            periodic_button = InlineKeyboardButton(
                "⏸ Остановить периодический анализ",
                callback_data="agent_stop_periodic",
            )
        else:
            periodic_button = InlineKeyboardButton(
                "▶️ Запустить периодический анализ",
                callback_data="agent_start_periodic",
            )
        keyboard = [
            [periodic_button],
            [InlineKeyboardButton("🔄 Запустить сейчас", callback_data="agent_run_now")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")],
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
                decisions = self.agent_decision_engine.make_decisions(underlying)
                
                self.agent_last_run[user_id] = datetime.now()
                
                found = False
                for item in decisions:
                    decision = item.get("decision")
                    if decision:
                        found = True
                        self.agent_last_signal[user_id] = decision
                        await self._send_agent_signal(chat_id, decision, context)
                if not found:
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
        threshold_type = signal.get('threshold_type')
        dte_bucket = signal.get('dte_bucket')
        
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
        if threshold_type:
            threshold_type_escaped = escape_html(str(threshold_type))
            message += f"<b>Пороги:</b> {threshold_type_escaped}"
            if dte_bucket:
                message += f" (DTE бин: {escape_html(str(dte_bucket))})"
            message += "\n"
        
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

    async def _send_agent_no_signal(
        self,
        chat_id: int,
        underlying: str,
        expiration: Optional[str],
        threshold_type: Optional[str],
        context: ContextTypes.DEFAULT_TYPE
    ):
        """Отправить сообщение о завершении анализа без сигнала для экспирации."""
        expiration_text = f", экспирация {expiration}" if expiration else ""
        threshold_text = f"Пороги: {threshold_type}" if threshold_type else "Пороги: статические"
        message = (
            f"ℹ️ <b>Анализ завершен</b>\n\n"
            f"Базовый актив: {escape_html(underlying)}{escape_html(expiration_text)}\n"
            f"{escape_html(threshold_text)}\n"
            f"Подходящих условий для входа не найдено."
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='HTML'
        )
    
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
            
            if callback_data in ("agent_toggle", "agent_start_periodic", "agent_stop_periodic"):
                already_on = self.agent_enabled.get(user_id, False)
                if callback_data == "agent_start_periodic":
                    want_on = True
                elif callback_data == "agent_stop_periodic":
                    want_on = False
                else:
                    want_on = not already_on  # legacy toggle

                if want_on:
                    self.agent_enabled[user_id] = True
                    interval_minutes = AGENT_CONFIG.get("run_interval_minutes", 60)
                    if context.job_queue:
                        for job in context.job_queue.get_jobs_by_name(f"agent_{user_id}"):
                            job.schedule_removal()
                        context.job_queue.run_repeating(
                            self._run_agent_periodic,
                            interval=interval_minutes * 60,
                            first=10,
                            name=f"agent_{user_id}",
                            data={"user_id": user_id, "chat_id": chat_id},
                        )
                        logger.info(
                            "Периодический анализ запущен (user=%s, interval=%s мин)",
                            user_id,
                            interval_minutes,
                        )
                    else:
                        logger.error("JobQueue недоступна в context")
                else:
                    self.agent_enabled[user_id] = False
                    if context.job_queue:
                        for job in context.job_queue.get_jobs_by_name(f"agent_{user_id}"):
                            job.schedule_removal()
                    logger.info("Периодический анализ остановлен (user=%s)", user_id)

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
                    no_signals = []
                    errors = []
                    
                    for underlying in UNDERLYING_ASSETS:
                        try:
                            logger.info(f"🤖 Немедленный запуск анализа агента для {underlying} (пользователь {user_id})")
                            decisions = self.agent_decision_engine.make_decisions(underlying)
                            self.agent_last_run[user_id] = datetime.now()
                            
                            if not decisions:
                                logger.info(f"ℹ️ Анализ завершен для {underlying}. Подходящих условий не найдено.")
                                no_signals.append((underlying, None, None))
                                continue
                            
                            for item in decisions:
                                decision = item.get("decision")
                                expiration = item.get("expiration")
                                threshold_type = item.get("threshold_type")
                                if decision:
                                    self.agent_last_signal[user_id] = decision
                                    signals_found.append((underlying, decision))
                                    await self._send_agent_signal(chat_id, decision, context)
                                    logger.info(f"✅ Сигнал найден для {underlying} {expiration}")
                                else:
                                    no_signals.append((underlying, expiration, threshold_type))
                                    await self._send_agent_no_signal(
                                        chat_id,
                                        underlying,
                                        expiration,
                                        threshold_type,
                                        context
                                    )
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
        В боте подписка на WS не выполняется:
        единственный источник WebSocket — api-server.
        """
        logger.info("WS в telegram-bot отключён: используем live-данные только из api-server")

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
                    CallbackQueryHandler(self.handle_day_selection, pattern="^option_day_"),
                    CallbackQueryHandler(self.handle_back_to_underlying, pattern="^option_back_underlying$"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel$")
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
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_start_periodic|agent_stop_periodic|agent_run_now)$")
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
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_start_periodic|agent_stop_periodic|agent_run_now)$")
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
                CallbackQueryHandler(self._handle_agent_callback, pattern="^(agent_toggle|agent_start_periodic|agent_stop_periodic|agent_run_now)$")
            ]
        )

        # Кнопка «Меню» и команда /menu — быстрый доступ в главное меню без лишнего шага с /start
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.Regex(f"^({re.escape(MENU_BUTTON_TEXT)}|Меню)$"),
                self.show_main_menu_for_message,
            )
        )
        application.add_handler(CommandHandler("menu", self.show_main_menu_for_message))
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
        # ConversationHandler для добавления пресета GEX (тикер + дата экспирации)
        gex_add_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.gex_start_add_preset, pattern="^gex_add_preset$")
            ],
            states={
                GEX_CHOOSING_UNDERLYING: [
                    CallbackQueryHandler(self.gex_handle_underlying, pattern="^gex_underlying_"),
                    CallbackQueryHandler(self.gex_show_menu, pattern="^gex_menu$")
                ],
                GEX_ENTERING_DAY: [
                    CallbackQueryHandler(self.gex_handle_day_selection, pattern="^gex_day_"),
                    CallbackQueryHandler(self.gex_show_menu, pattern="^gex_menu$")
                ],
                GEX_CHOOSING_MONTH: [
                    CallbackQueryHandler(self.gex_handle_month, pattern="^gex_month_"),
                    CallbackQueryHandler(self.gex_show_menu, pattern="^gex_menu$")
                ]
            },
            fallbacks=[
                CallbackQueryHandler(self.gex_show_menu, pattern="^gex_menu$"),
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
            ]
        )
        application.add_handler(gex_add_conv)

        # ConversationHandler для мониторинга GEX (порог, частота, запуск/остановка)
        gex_monitor_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.gex_monitor_show_menu, pattern="^gex_monitor_menu$")
            ],
            states={
                GEX_MONITOR_MENU: [
                    CallbackQueryHandler(self.gex_monitor_handle_button, pattern="^gex_monitor_")
                ],
                GEX_MONITOR_ENTER_THRESHOLD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.gex_monitor_handle_threshold_input)
                ],
                GEX_MONITOR_ENTER_INTERVAL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.gex_monitor_handle_interval_input)
                ],
            },
            fallbacks=[
                CallbackQueryHandler(self.gex_monitor_fallback, pattern="^gex_monitor_(back|menu)$"),
                CallbackQueryHandler(self.back_to_main_menu, pattern="^back_to_menu$")
            ]
        )
        application.add_handler(gex_monitor_conv)

        application.add_handler(add_option_conv)
        application.add_handler(remove_option_conv)
        application.add_handler(set_levels_conv)

        # ВАЖНО: Специфичные обработчики должны быть ПЕРЕД общим обработчиком
        # Обработчик callback для кнопок агента (должен быть первым, чтобы не перехватывался общим обработчиком)
        agent_callback_handler = CallbackQueryHandler(
            self._handle_agent_callback,
            pattern="^(agent_toggle|agent_start_periodic|agent_stop_periodic|agent_run_now)$"
        )
        application.add_handler(agent_callback_handler)
        logger.info("✅ Обработчик кнопок агента зарегистрирован (agent_toggle, agent_run_now)")
        
        # Обработчик callback для кнопок главного меню (общий обработчик - в конце)
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^active_signals$"))
        logger.info("✅ Общий обработчик callback зарегистрирован")


        # Команды в меню Telegram (иконка рядом с полем ввода) — «Меню» первым для быстрого доступа
        async def set_bot_commands(app: Application):
            await app.bot.set_my_commands([
                BotCommand("menu", "Главное меню"),
                BotCommand("start", "Старт"),
                BotCommand("help", "Помощь"),
            ])
        application.post_init = set_bot_commands

        # Запускаем бота: режим выбирается через BOT_CONFIG (config.py)
        mode = (BOT_CONFIG.get("mode") or "polling").lower()
        drop_pending = bool(BOT_CONFIG.get("drop_pending_updates", True))

        if mode == "webhook":
            webhook_url = BOT_CONFIG.get("webhook_url") or ""
            webhook_path = BOT_CONFIG.get("webhook_path") or "/telegram-webhook"
            listen = BOT_CONFIG.get("webhook_listen") or "0.0.0.0"
            port = int(BOT_CONFIG.get("webhook_port") or 8443)
            secret = BOT_CONFIG.get("webhook_secret_token") or None
            if not webhook_url:
                logger.error(
                    "BOT_MODE=webhook, но WEBHOOK_URL не задан в .env. Откат к polling."
                )
                mode = "polling"
            else:
                full_webhook_url = f"{webhook_url.rstrip('/')}{webhook_path}"
                logger.info(
                    "Бот запущен в режиме webhook: %s (listen=%s:%s, secret=%s)",
                    full_webhook_url,
                    listen,
                    port,
                    "set" if secret else "none",
                )
                application.run_webhook(
                    listen=listen,
                    port=port,
                    url_path=webhook_path.lstrip("/"),
                    webhook_url=full_webhook_url,
                    secret_token=secret,
                    drop_pending_updates=drop_pending,
                    allowed_updates=Update.ALL_TYPES,
                )
                return

        logger.info("Бот запущен в режиме polling")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=drop_pending,
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