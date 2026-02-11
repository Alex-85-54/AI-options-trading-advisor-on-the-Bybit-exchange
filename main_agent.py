"""
Главный модуль для периодического запуска торгового агента
Использует APScheduler для планирования запусков агента
"""
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Добавляем корень проекта в путь для импортов
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from config import AGENT_CONFIG, CONFIG
from core.agent.decision_engine import get_decision_engine
from services.data_store import data_store

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# Базовые активы для анализа
UNDERLYING_ASSETS = ["BTC"]  # Можно расширить: ["BTC", "ETH", "SOL"]


class AgentScheduler:
    """Класс для управления периодическим запуском торгового агента"""
    
    def __init__(self):
        """Инициализация планировщика агента"""
        self.scheduler = BlockingScheduler()
        self.decision_engine = get_decision_engine(data_store=data_store)
        self.run_interval_minutes = AGENT_CONFIG.get("run_interval_minutes", 60)
        self.running = False
        
        # Статистика работы
        self.stats = {
            'total_runs': 0,
            'successful_runs': 0,
            'failed_runs': 0,
            'signals_generated': 0,
            'last_run_time': None,
            'last_signal_time': None
        }
    
    def run_agent_analysis(self):
        """
        Запуск анализа агента для всех активов
        
        Этот метод вызывается планировщиком периодически
        """
        logger.info("=" * 60)
        logger.info(f"🤖 Запуск анализа торгового агента - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        self.stats['total_runs'] += 1
        self.stats['last_run_time'] = datetime.now()
        
        signals_generated = 0
        
        try:
            for underlying in UNDERLYING_ASSETS:
                try:
                    logger.info(f"📊 Анализ актива: {underlying}")
                    
                    # Запускаем принятие решения
                    decisions = self.decision_engine.make_decisions(underlying)
                    found = False
                    for item in decisions:
                        decision = item.get("decision")
                        expiration = item.get("expiration")
                        if decision:
                            found = True
                            signals_generated += 1
                            self.stats['signals_generated'] += 1
                            self.stats['last_signal_time'] = datetime.now()
                            
                            signal_type = decision.get('signal_type', 'unknown')
                            confidence = decision.get('confidence', 0)
                            reasoning = decision.get('reasoning', '')[:100]
                            
                            logger.info(
                                f"✅ Сигнал сгенерирован для {underlying} {expiration}:\n"
                                f"   Тип: {signal_type}\n"
                                f"   Уверенность: {confidence:.0%}\n"
                                f"   Обоснование: {reasoning}..."
                            )
                            logger.debug(f"Полный сигнал: {decision}")
                    if not found:
                        logger.info(f"ℹ️ Подходящих условий для {underlying} не найдено")
                    
                except Exception as e:
                    logger.error(
                        f"❌ Ошибка при анализе {underlying}: {e}",
                        exc_info=True
                    )
                    self.stats['failed_runs'] += 1
                    continue
            
            self.stats['successful_runs'] += 1
            
            logger.info(
                f"✅ Анализ завершен. Сигналов сгенерировано: {signals_generated}"
            )
            logger.info(f"📊 Статистика: успешных запусков: {self.stats['successful_runs']}, "
                       f"ошибок: {self.stats['failed_runs']}, "
                       f"всего сигналов: {self.stats['signals_generated']}")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка при запуске агента: {e}", exc_info=True)
            self.stats['failed_runs'] += 1
    
    def start(self, run_at_hour_start: bool = True):
        """
        Запустить планировщик агента
        
        Args:
            run_at_hour_start: Если True, запускать в начале каждого часа (10:00, 11:00, ...)
                              Если False, запускать с заданным интервалом
        """
        if self.running:
            logger.warning("Планировщик агента уже запущен")
            return
        
        logger.info("🚀 Запуск планировщика торгового агента...")
        logger.info(f"⚙️ Конфигурация:")
        logger.info(f"   - Интервал запуска: {self.run_interval_minutes} минут")
        logger.info(f"   - Запуск в начале часа: {run_at_hour_start}")
        logger.info(f"   - Активы для анализа: {UNDERLYING_ASSETS}")
        logger.info(f"   - Мин. уверенность: {AGENT_CONFIG.get('min_confidence', 0.6):.0%}")
        
        if run_at_hour_start:
            # Запуск в начале каждого часа (например, 10:00, 11:00, 12:00)
            # Вычисляем время следующего запуска (начало следующего часа)
            now = datetime.now()
            next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            
            logger.info(f"📅 Первый запуск запланирован на: {next_hour.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Добавляем задачу с cron-триггером (каждый час в 0 минут)
            self.scheduler.add_job(
                self.run_agent_analysis,
                trigger=CronTrigger(minute=0),  # В начале каждого часа
                id='agent_analysis',
                replace_existing=True,
                max_instances=1,  # Только один экземпляр может выполняться одновременно
                coalesce=True  # Если пропущено несколько запусков, выполнить только один
            )
        else:
            # Запуск с заданным интервалом
            logger.info(f"📅 Запуск каждые {self.run_interval_minutes} минут")
            
            self.scheduler.add_job(
                self.run_agent_analysis,
                trigger=IntervalTrigger(minutes=self.run_interval_minutes),
                id='agent_analysis',
                replace_existing=True,
                max_instances=1,
                coalesce=True
            )
        
        # Запускаем планировщик
        self.running = True
        
        try:
            logger.info("✅ Планировщик агента запущен. Ожидание запланированных запусков...")
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("⏸ Остановка планировщика агента...")
            self.stop()
    
    def stop(self):
        """Остановить планировщик агента"""
        if not self.running:
            return
        
        logger.info("⏸ Остановка планировщика агента...")
        self.scheduler.shutdown(wait=True)
        self.running = False
        
        logger.info("📊 Финальная статистика:")
        logger.info(f"   - Всего запусков: {self.stats['total_runs']}")
        logger.info(f"   - Успешных: {self.stats['successful_runs']}")
        logger.info(f"   - С ошибками: {self.stats['failed_runs']}")
        logger.info(f"   - Сигналов сгенерировано: {self.stats['signals_generated']}")
        logger.info("✅ Планировщик остановлен")
    
    def get_stats(self) -> dict:
        """Получить статистику работы агента"""
        return self.stats.copy()


def main():
    """Главная функция для запуска планировщика агента"""
    # Проверяем наличие API ключа
    api_key = AGENT_CONFIG.get("deepseek_api_key", "")
    if not api_key:
        logger.warning(
            "⚠️ DEEPSEEK_API_KEY не установлен. "
            "Агент не сможет делать запросы к LLM. "
            "Установите переменную окружения DEEPSEEK_API_KEY."
        )
    
    # Создаем и запускаем планировщик
    agent_scheduler = AgentScheduler()
    
    # Настройка обработчиков сигналов для корректного завершения
    def signal_handler(signum, frame):
        logger.info(f"Получен сигнал {signum}, останавливаем планировщик...")
        agent_scheduler.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Запускаем планировщик
    # run_at_hour_start=True - запуск в начале каждого часа (10:00, 11:00, ...)
    # run_at_hour_start=False - запуск каждые N минут
    run_at_hour_start = AGENT_CONFIG.get("run_at_hour_start", True)
    
    try:
        agent_scheduler.start(run_at_hour_start=run_at_hour_start)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
