"""
Модуль для буферизации логов в памяти для веб-интерфейса администратора
"""
import logging
import threading
from typing import List, Dict
from datetime import datetime
from collections import deque


class LogBufferHandler(logging.Handler):
    """
    Кастомный handler для логирования, который хранит последние N логов в памяти
    для отображения в веб-интерфейсе администратора
    """
    
    def __init__(self, max_logs: int = 1000):
        """
        Инициализация handler
        
        Args:
            max_logs: Максимальное количество хранимых логов (по умолчанию 1000)
        """
        super().__init__()
        self.max_logs = max_logs
        self.logs: deque = deque(maxlen=max_logs)
        self.lock = threading.Lock()
    
    def emit(self, record: logging.LogRecord):
        """Обработка записи лога"""
        try:
            # Форматируем запись
            log_entry = {
                'timestamp': datetime.fromtimestamp(record.created).isoformat(),
                'level': record.levelname,
                'logger': record.name,
                'message': self.format(record),
                'module': record.module,
                'funcName': record.funcName,
                'lineno': record.lineno
            }
            
            # Добавляем в буфер (thread-safe)
            with self.lock:
                self.logs.append(log_entry)
        except Exception:
            # Игнорируем ошибки в handler, чтобы не нарушить работу приложения
            self.handleError(record)
    
    def get_logs(self, limit: int = None, level: str = None, logger_name: str = None) -> List[Dict]:
        """
        Получить логи из буфера
        
        Args:
            limit: Максимальное количество логов (если None, возвращаются все)
            level: Фильтр по уровню (INFO, WARNING, ERROR и т.д.)
            logger_name: Фильтр по имени logger
            
        Returns:
            Список словарей с логами
        """
        with self.lock:
            logs = list(self.logs)
        
        # Фильтрация
        if level:
            logs = [log for log in logs if log['level'] == level.upper()]
        
        if logger_name:
            logs = [log for log in logs if logger_name.lower() in log['logger'].lower()]
        
        # Лимит
        if limit:
            logs = logs[-limit:]  # Последние N логов
        
        return logs
    
    def clear(self):
        """Очистить буфер логов"""
        with self.lock:
            self.logs.clear()
    
    def count_by_level(self) -> Dict[str, int]:
        """Подсчитать количество логов по уровням"""
        with self.lock:
            counts = {}
            for log in self.logs:
                level = log['level']
                counts[level] = counts.get(level, 0) + 1
            return counts


# Глобальный экземпляр handler (singleton)
_log_buffer_handler: LogBufferHandler = None


def get_log_buffer_handler(max_logs: int = 1000) -> LogBufferHandler:
    """
    Получить глобальный экземпляр LogBufferHandler
    
    Args:
        max_logs: Максимальное количество логов (используется только при первом вызове)
        
    Returns:
        Экземпляр LogBufferHandler
    """
    global _log_buffer_handler
    if _log_buffer_handler is None:
        _log_buffer_handler = LogBufferHandler(max_logs=max_logs)
    return _log_buffer_handler
