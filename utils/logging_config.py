"""
Модуль для настройки логирования с ротацией файлов
"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional

# Глобальная переменная для отслеживания, был ли уже настроен root logger
_root_logger_configured = False


def setup_logging(
    service_name: str,
    log_level: int = logging.INFO,
    log_dir: str = "logs",
    max_file_size_mb: int = 100,
    backup_count: int = 5,
    format_string: Optional[str] = None
) -> logging.Logger:
    """
    Настроить логирование для сервиса с ротацией файлов
    
    Args:
        service_name: Имя сервиса (используется в имени файла лога, например 'telegram_bot')
        log_level: Уровень логирования (по умолчанию INFO)
        log_dir: Директория для хранения логов (по умолчанию 'logs')
        max_file_size_mb: Максимальный размер одного файла лога в МБ (по умолчанию 100 МБ)
        backup_count: Количество резервных файлов при ротации (по умолчанию 5)
        format_string: Формат строки лога (если None, используется формат по умолчанию)
    
    Returns:
        Настроенный logger для сервиса
    """
    global _root_logger_configured
    
    # Создаем директорию для логов, если её нет
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Имя файла лога для конкретного сервиса
    log_file = log_path / f"{service_name}.log"
    
    # Формат строки по умолчанию
    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    formatter = logging.Formatter(format_string)
    
    # Настраиваем root logger только один раз
    root_logger = logging.getLogger()
    
    if not _root_logger_configured:
        root_logger.setLevel(log_level)
        
        # Удаляем существующие handlers root logger (если есть)
        # Сохраняем только те, которые не относятся к файлам или stdout
        for handler in root_logger.handlers[:]:
            if isinstance(handler, (RotatingFileHandler, logging.FileHandler, logging.StreamHandler)):
                root_logger.removeHandler(handler)
                handler.close()
        
        # Handler для вывода в stdout (общий для всех сервисов)
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)
        
        _root_logger_configured = True
    
    # Получаем logger для конкретного сервиса
    logger = logging.getLogger(service_name)
    logger.setLevel(log_level)
    
    # Удаляем существующие handlers для этого logger (чтобы не было дублирования)
    for handler in logger.handlers[:]:
        if isinstance(handler, (RotatingFileHandler, logging.FileHandler, logging.StreamHandler)):
            logger.removeHandler(handler)
            handler.close()
    
    # Handler для записи в файл с ротацией (отдельный файл для каждого сервиса)
    max_bytes = max_file_size_mb * 1024 * 1024
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Handler для вывода в stdout (дублирует вывод в консоль)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)
    
    # Отключаем propagate, чтобы логи сервиса не передавались в root logger
    # (это предотвращает дублирование и гарантирует, что каждый сервис пишет только в свой файл)
    logger.propagate = False
    
    return logger


def setup_service_logging(service_name: str, log_level: int = logging.INFO) -> logging.Logger:
    """
    Упрощенная функция для настройки логирования сервиса
    
    Args:
        service_name: Имя сервиса
        log_level: Уровень логирования (по умолчанию INFO)
    
    Returns:
        Настроенный logger
    """
    return setup_logging(
        service_name=service_name,
        log_level=log_level,
        log_dir="logs",
        max_file_size_mb=100,
        backup_count=5
    )
