# brain/settings.py
"""Хранение пользовательских настроек (провайдер перевода, API-ключ, модели).

Настройки живут в data/settings.json рядом с приложением. Этот файл уже попадает
в .gitignore (строка ``data/*``), поэтому API-ключ не утечёт в репозиторий.
"""

import json
import os

from brain.paths import data_dir, ensure_data_dir

SETTINGS_FILE = os.path.join(data_dir(), "settings.json")

DEFAULT_SETTINGS = {
    "provider": "ollama",            # "ollama" | "deepseek"
    "ollama_model": "translategemma:4b",
    "deepseek_model": "deepseek-chat",
    "api_key": "",
}


def load_settings():
    """Возвращает настройки: переменные окружения > settings.json > значения по умолчанию."""
    ensure_data_dir()

    settings = dict(DEFAULT_SETTINGS)

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                settings.update(data)
        except Exception:
            pass

    # Переменные окружения (их выставляет gui_launcher.py) имеют приоритет над файлом.
    env = os.environ
    if env.get("GADGET_PROVIDER"):
        settings["provider"] = env["GADGET_PROVIDER"]
    if env.get("GADGET_API_KEY"):
        settings["api_key"] = env["GADGET_API_KEY"]
    if env.get("GADGET_OLLAMA_MODEL"):
        settings["ollama_model"] = env["GADGET_OLLAMA_MODEL"]
    if env.get("GADGET_DEEPSEEK_MODEL"):
        settings["deepseek_model"] = env["GADGET_DEEPSEEK_MODEL"]

    if settings.get("provider") not in ("ollama", "deepseek"):
        settings["provider"] = "ollama"

    return settings


def save_settings(settings):
    """Сохраняет настройки в data/settings.json и возвращает итоговый словарь."""
    ensure_data_dir()

    data = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        data.update(settings)

    if data.get("provider") not in ("ollama", "deepseek"):
        data["provider"] = "ollama"

    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return data
