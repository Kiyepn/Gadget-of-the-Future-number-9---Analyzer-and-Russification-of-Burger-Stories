# brain/paths.py
"""Централизованное определение путей для обычного запуска и замороженного .exe.

Почему это отдельный модуль с «умным» выбором папки data/:

1. Раньше все данные (память, словари, настройки, кэш) всегда писались в
   <папка приложения>/data. Если exe лежит в Program Files (или в любой другой
   защищённой папке), запись молча падала, и пользователь не понимал, «куда
   девается память».
2. Теперь порядок такой:
      GADGET_DATA_DIR (переменная окружения, если задана) →
      <папка приложения>/data (portable-режим, если туда реально можно писать) →
      %LOCALAPPDATA%/GadgetOfTheFuture/data (надёжный резерв для установленных копий).
3. Найденный путь кэшируется и попадает в лог, чтобы всегда было видно, где
   лежат данные.
"""
import importlib.util
import os
import sys


APP_NAME = "GadgetOfTheFuture"
_CONFIG_MODULE_NAME = "app_config"

_resolved_data_dir = None


def app_root():
    """Корневая папка приложения (где лежат config.py и data/)."""
    if getattr(sys, "frozen", False):
        # Замороженное приложение: рядом с .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    # Dev-режим: этот файл лежит в <root>/brain/paths.py, поэтому на уровень выше.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_appdata_dir():
    """Резервная папка в профиле пользователя (всегда доступна на запись)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.expanduser(os.path.join("~", "AppData", "Local"))
    return os.path.join(base, APP_NAME)


def _make_writable_dir(path):
    """Создаёт папку и проверяет, что в неё реально можно писать.

    Возвращает True, только если удалось создать и записать пробный файл.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        try:
            os.remove(probe)
        except OSError:
            pass
        return True
    except OSError:
        return False


def resolve_data_dir(force=False):
    """Выбирает и запоминает рабочую папку data/."""
    global _resolved_data_dir
    if _resolved_data_dir and not force:
        return _resolved_data_dir

    candidates = []

    # 1. Явный override — главный инструмент для тестов и переносимых сборок.
    env_override = (os.environ.get("GADGET_DATA_DIR") or "").strip()
    if env_override:
        candidates.append(os.path.abspath(os.path.expanduser(env_override)))

    # 2. Portable: рядом с приложением.
    candidates.append(os.path.join(app_root(), "data"))

    # 3. Резерв в профиле пользователя.
    candidates.append(os.path.join(_local_appdata_dir(), "data"))

    for candidate in candidates:
        if _make_writable_dir(candidate):
            _resolved_data_dir = candidate
            return candidate

    # Не удалось никуда записать — возвращаем последний вариант, а ошибки
    # пусть обрабатывает тот, кто пишет. Так приложение хотя бы не падает.
    _resolved_data_dir = candidates[-1]
    return _resolved_data_dir


def data_dir():
    """Папка data/ (выбранная один раз за запуск)."""
    return resolve_data_dir()


def is_portable_data_dir():
    """True, если данные лежат рядом с приложением (portable-режим)."""
    return os.path.normcase(data_dir()) == os.path.normcase(
        os.path.join(app_root(), "data")
    )


def data_location_hint():
    """Короткое человекочитаемое описание, куда пишутся данные."""
    path = data_dir()
    if is_portable_data_dir():
        return "рядом с программой: " + path
    return "в профиле пользователя: " + path


def ensure_data_dir():
    """Создаёт папку data/, если её ещё нет. Возвращает путь к ней."""
    d = data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def data_file(name):
    """Путь к файлу внутри папки data/."""
    ensure_data_dir()
    return os.path.join(data_dir(), name)


def config_path():
    """Путь к config.py рядом с приложением."""
    return os.path.join(app_root(), "config.py")


def load_config():
    """Динамически загружает config.py из корневой папки приложения.

    config.py НЕ запаковывается внутрь .exe, а лежит рядом с ним.
    Возвращает объект модуля, чтобы можно было обращаться как config.XXX.
    """
    path = config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            "Файл config.py не найден рядом с исполняемым файлом: " + path
        )
    spec = importlib.util.spec_from_file_location(_CONFIG_MODULE_NAME, path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg
