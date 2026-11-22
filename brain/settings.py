# brain/settings.py
"""Пользовательские настройки: провайдер перевода, ключ, параметры оверлея.

Файл: <data>/settings.json (попадает в .gitignore, в релизную сборку не
копируется). API-ключ DeepSeek хранится зашифрованным через DPAPI — см.
brain/secrets_dpapi.py.

В v1.4.2 добавлены настройки оверлея, потому что интерфейс переехал из
консоли в отдельное окно поверх игры.
"""
import json
import logging
import os

from brain import secrets_dpapi
from brain.ocr_providers import DEFAULT_PROVIDER, PROVIDERS
from brain.paths import data_file, ensure_data_dir

_ENC_PREFIX = "dpapi:"

DEFAULT_SETTINGS = {
    # --- провайдер перевода ---
    "provider": "ollama",                     # "ollama" | "deepseek"
    "ollama_model": "translategemma:4b",
    "deepseek_model": "deepseek-chat",
    "api_key": "",                            # в файле лежит в виде "dpapi:<base64>"
    "ocr_provider": DEFAULT_PROVIDER,         # "windows" (по умолчанию) | "easyocr"
    "ocr_gpu": "auto",                        # "auto" | "gpu" | "cpu" (только easyocr)

    # --- оверлей перевода ---
    "overlay_enabled": True,
    "overlay_position": "bottom-center",      # см. OVERLAY_POSITIONS
    "overlay_opacity": 0.93,                  # 0.3 .. 1.0
    "overlay_font_size": 18,                  # 11 .. 40
    "overlay_width": 640,                     # 320 .. 1400
    "overlay_duration": 12.0,                 # секунд до автоскрытия
    "overlay_show_source": True,              # показывать оригинал EN
    "overlay_accent": "violet",               # = launcher_theme (единая тема)

    # --- лаунчер ---
    "launcher_theme": "violet",               # см. LAUNCHER_THEMES
    "create_shortcut": True,                  # ярлык на рабочем столе (собранный exe)

    # --- прочее ---
    "log_level": "INFO",
}

OVERLAY_POSITIONS = (
    "bottom-center", "top-center", "center",
    "top-left", "top-right", "bottom-left", "bottom-right",
)
OVERLAY_ACCENTS = ("violet", "blue", "green", "orange", "crimson", "mono")
LAUNCHER_THEMES = ("violet", "blue", "green", "orange", "crimson", "mono")


def settings_path():
    return data_file("settings.json")


def _clamp(value, low, high, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _sanitize(settings):
    """Приводит значения к допустимым диапазонам, чтобы кривой JSON не ломал UI."""
    data = dict(DEFAULT_SETTINGS)
    data.update({k: v for k, v in settings.items() if v is not None})

    if data.get("provider") not in ("ollama", "deepseek"):
        data["provider"] = "ollama"
    ocr_provider = data.get("ocr_provider")
    if isinstance(ocr_provider, str):
        ocr_provider = ocr_provider.strip().lower()
    if ocr_provider not in PROVIDERS:
        ocr_provider = DEFAULT_PROVIDER
    data["ocr_provider"] = ocr_provider
    if data.get("ocr_gpu") not in ("auto", "gpu", "cpu"):
        data["ocr_gpu"] = "auto"
    if data.get("overlay_position") not in OVERLAY_POSITIONS:
        data["overlay_position"] = DEFAULT_SETTINGS["overlay_position"]

    # Единая система оформления: тема лаунчера задаёт и цвет карточки перевода.
    # Старые файлы могли иметь только overlay_accent — используем его как тему,
    # чтобы после обновления цвет не «сбросился» на фиолетовый.
    theme = data.get("launcher_theme")
    if not (isinstance(theme, str) and theme.strip()):
        theme = data.get("overlay_accent")
    if isinstance(theme, str):
        theme = theme.strip().lower()
    if theme not in LAUNCHER_THEMES:
        theme = DEFAULT_SETTINGS["launcher_theme"]
    data["launcher_theme"] = theme
    data["overlay_accent"] = theme
    data["create_shortcut"] = bool(data.get("create_shortcut", True))

    data["overlay_enabled"] = bool(data.get("overlay_enabled", True))
    data["overlay_show_source"] = bool(data.get("overlay_show_source", True))
    data["overlay_opacity"] = round(
        _clamp(data.get("overlay_opacity"), 0.3, 1.0, DEFAULT_SETTINGS["overlay_opacity"]), 2
    )
    data["overlay_font_size"] = int(
        _clamp(data.get("overlay_font_size"), 11, 40, DEFAULT_SETTINGS["overlay_font_size"])
    )
    data["overlay_width"] = int(
        _clamp(data.get("overlay_width"), 320, 1400, DEFAULT_SETTINGS["overlay_width"])
    )
    data["overlay_duration"] = round(
        _clamp(data.get("overlay_duration"), 2.0, 120.0, DEFAULT_SETTINGS["overlay_duration"]), 1
    )

    for key in ("ollama_model", "deepseek_model"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            data[key] = DEFAULT_SETTINGS[key]

    if not isinstance(data.get("api_key"), str):
        data["api_key"] = ""
    return data


def _store_api_key(data):
    """Шифрует api_key перед записью на диск (или оставляет как есть)."""
    key = data.get("api_key") or ""
    data.pop("api_key_enc", None)
    if not key:
        data["api_key"] = ""
        return data
    if key.startswith(_ENC_PREFIX):
        # Уже зашифровано — не шифруем повторно.
        data["api_key"] = key
        return data
    encrypted = secrets_dpapi.protect(key)
    if encrypted:
        data["api_key"] = _ENC_PREFIX + encrypted
    else:
        logging.getLogger(__name__).warning(
            "DPAPI недоступен: API-ключ сохранён в открытом виде."
        )
    return data


def _load_api_key(raw):
    """Расшифровывает ключ, если он зашифрован; поддерживает старый формат."""
    if not raw:
        return ""
    if raw.startswith(_ENC_PREFIX):
        decrypted = secrets_dpapi.unprotect(raw[len(_ENC_PREFIX):])
        if decrypted is None:
            logging.getLogger(__name__).warning(
                "Не удалось расшифровать API-ключ (другой пользователь/машина)."
            )
            return ""
        return decrypted
    return raw  # старый открытый формат — читаем как есть


def load_settings():
    """Возвращает настройки: переменные окружения > settings.json > дефолты."""
    ensure_data_dir()
    settings = dict(DEFAULT_SETTINGS)

    path = settings_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                raw_key = stored.pop("api_key", "")
                settings.update({k: v for k, v in stored.items() if v is not None})
                settings["api_key"] = _load_api_key(raw_key)
        except Exception as exc:
            logging.getLogger(__name__).warning("Не удалось прочитать settings.json: %s", exc)

    env = os.environ
    if env.get("GADGET_PROVIDER"):
        settings["provider"] = env["GADGET_PROVIDER"]
    if env.get("GADGET_API_KEY"):
        settings["api_key"] = env["GADGET_API_KEY"]
    if env.get("GADGET_OLLAMA_MODEL"):
        settings["ollama_model"] = env["GADGET_OLLAMA_MODEL"]
    if env.get("GADGET_DEEPSEEK_MODEL"):
        settings["deepseek_model"] = env["GADGET_DEEPSEEK_MODEL"]
    if env.get("GADGET_OCR_PROVIDER"):
        settings["ocr_provider"] = env["GADGET_OCR_PROVIDER"]

    return _sanitize(settings)


def save_settings(settings):
    """Сохраняет настройки в settings.json (атомарно) и возвращает итог."""
    ensure_data_dir()
    data = _sanitize(settings)
    data = _store_api_key(data)

    path = settings_path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        logging.getLogger(__name__).error("Не удалось сохранить settings.json: %s", exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    # Возвращаем настройки с расшифрованным ключом — их использует лаунчер.
    result = dict(data)
    result["api_key"] = _load_api_key(data.get("api_key", ""))
    return _sanitize(result)
