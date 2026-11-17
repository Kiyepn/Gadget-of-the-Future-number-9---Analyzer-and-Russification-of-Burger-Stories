# brain/logging_setup.py
"""Логирование в файл + безопасный stdout.

Зачем: собранный exe собирается в режиме --windowed, то есть консоли у него
нет и sys.stdout == None. Раньше код просто делал print(), из-за чего логи
переводчика терялись. Теперь всё пишется в data/gadget.log и параллельно в
stdout, если он есть.
"""
import logging
import logging.handlers
import os
import sys

from brain.paths import data_dir

LOG_FILE_NAME = "gadget.log"
_MAX_BYTES = 1 * 1024 * 1024
_BACKUP_COUNT = 2

_configured = False


def log_file_path():
    """Путь к файлу лога (в папке data/)."""
    return os.path.join(data_dir(), LOG_FILE_NAME)


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler, который не падает и не печатает мусор при stdout=None."""

    def emit(self, record):
        stream = self.stream
        if stream is None:
            return
        try:
            super().emit(record)
        except Exception:
            pass


def setup_logging(level=logging.INFO):
    """Настраивает корневой логгер (идемпотентно). Возвращает путь к логу."""
    global _configured
    path = log_file_path()
    if _configured:
        return path

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        # Если файл лога недоступен — приложение всё равно должно работать.
        pass

    # В консоль пишем, только если консоль реально существует.
    stdout = sys.stdout
    if stdout is not None:
        try:
            stream_handler = _SafeStreamHandler(stdout)
            stream_handler.setFormatter(fmt)
            root.addHandler(stream_handler)
        except Exception:
            pass

    _configured = True
    return path


def log(message, level=logging.INFO):
    """Короткий помощник: печатает в лог и в консоль (если она есть)."""
    logging.getLogger("gadget").log(level, message)
