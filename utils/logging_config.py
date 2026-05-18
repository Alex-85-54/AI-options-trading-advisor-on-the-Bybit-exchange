"""
Настройка логирования с ротацией файлов.

Уровень и параметры берутся из config.LOGGING_CONFIG, его можно переопределить
переменными окружения LOG_LEVEL, LOG_MAX_FILE_SIZE_MB, LOG_BACKUP_COUNT.
Шумные сторонние библиотеки (httpcore, httpx, telegram.* и т.п.) автоматически
заглушаются согласно LOGGING_CONFIG["noisy_loggers"].
"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional, Union

try:
    from config import LOGGING_CONFIG as _CFG_LOGGING_CONFIG
except Exception:
    _CFG_LOGGING_CONFIG = {}


_root_logger_configured = False
_noisy_loggers_applied = False


def _resolve_level(level: Union[int, str, None], default: int = logging.INFO) -> int:
    if level is None:
        return default
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        return logging.getLevelName(level.upper()) if level else default
    return default


def _apply_noisy_loggers() -> None:
    """Заглушает шумные сторонние библиотеки. Вызывается один раз."""
    global _noisy_loggers_applied
    if _noisy_loggers_applied:
        return
    noisy = (_CFG_LOGGING_CONFIG or {}).get("noisy_loggers", {}) or {}
    for name, level in noisy.items():
        try:
            logging.getLogger(name).setLevel(_resolve_level(level, logging.WARNING))
        except Exception:
            pass
    _noisy_loggers_applied = True


def setup_logging(
    service_name: str,
    log_level: Optional[Union[int, str]] = None,
    log_dir: str = "logs",
    max_file_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    format_string: Optional[str] = None,
) -> logging.Logger:
    """
    Настроить логирование для сервиса с ротацией файлов.

    Если параметры не заданы явно, берутся из config.LOGGING_CONFIG.
    """
    global _root_logger_configured

    cfg = _CFG_LOGGING_CONFIG or {}
    level = _resolve_level(
        log_level if log_level is not None else cfg.get("level"),
        logging.INFO,
    )
    if max_file_size_mb is None:
        max_file_size_mb = int(cfg.get("max_file_size_mb", 20))
    if backup_count is None:
        backup_count = int(cfg.get("backup_count", 3))

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"{service_name}.log"

    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_string)

    root_logger = logging.getLogger()

    if not _root_logger_configured:
        root_logger.setLevel(level)
        for handler in root_logger.handlers[:]:
            if isinstance(handler, (RotatingFileHandler, logging.FileHandler, logging.StreamHandler)):
                root_logger.removeHandler(handler)
                handler.close()
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(level)
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)
        _root_logger_configured = True

    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    for handler in logger.handlers[:]:
        if isinstance(handler, (RotatingFileHandler, logging.FileHandler, logging.StreamHandler)):
            logger.removeHandler(handler)
            handler.close()

    max_bytes = max_file_size_mb * 1024 * 1024
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    logger.propagate = False

    _apply_noisy_loggers()

    return logger


def setup_service_logging(
    service_name: str,
    log_level: Optional[Union[int, str]] = None,
) -> logging.Logger:
    """
    Упрощённая обёртка: уровень и размеры берутся из config.LOGGING_CONFIG.

    Параметр log_level оставлен для совместимости со старыми вызовами,
    но передавать его не обязательно — рекомендуется управлять через config/.env.
    """
    return setup_logging(service_name=service_name, log_level=log_level)
