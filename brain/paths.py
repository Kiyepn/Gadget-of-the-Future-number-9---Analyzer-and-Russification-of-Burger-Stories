# brain/paths.py
"""Централизованное определение путей для обычного запуска и замороженного .exe.

Правила:
- В замороженном приложении (PyInstaller, sys.frozen == True) рабочие данные
  (config.py и папка data) лежат РЯДОМ с исполняемым файлом:
  os.path.dirname(sys.executable).
- В обычном режиме (python main.py) корень — это папка проекта.
"""
import importlib.util
import os
import sys


def app_root():
    """Корневая папка приложения (где лежат config.py и data/)."""
    if getattr(sys, 'frozen', False):
        # Замороженное приложение: рядом с .exe
        return os.path.dirname(sys.executable)
    # Dev-режим: этот файл лежит в <root>/brain/paths.py, поэтому на уровень выше.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Папка data/ рядом с приложением."""
    return os.path.join(app_root(), 'data')


def ensure_data_dir():
    """Создаёт папку data/, если её ещё нет. Возвращает путь к ней."""
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    """Путь к config.py рядом с приложением."""
    return os.path.join(app_root(), 'config.py')


def load_config():
    """Динамически загружает config.py из корневой папки приложения.

    config.py НЕ запаковывается внутрь .exe, а лежит рядом с ним.
    Возвращает объект модуля, чтобы можно было обращаться как config.XXX.
    """
    path = config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            'Файл config.py не найден рядом с исполняемым файлом: ' + path
        )
    spec = importlib.util.spec_from_file_location('app_config', path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg
