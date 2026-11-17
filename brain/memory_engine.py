# brain/memory_engine.py
"""Память диалога и лор персонажей.

Что изменилось в v1.4.2 и зачем:

* Все файлы теперь создаются через brain.paths.data_dir() — раньше путь
  вычислялся один раз при импорте, и если папка оказывалась недоступной,
  запись молча падала (пользователь «терял» память).
* Запись стала атомарной (temp-файл + os.replace): если процесс убьют в
  момент сохранения, файл не превратится в обрезанный JSON.
* Добавлен threading.Lock: перевод и фоновый анализ персонажей пишут
  одновременно из разных потоков, из-за чего записи терялись.
* Битый JSON больше не удаляется молча — он сохраняется как .corrupt-<ts>.
"""
import json
import os
import threading
import time

from brain.paths import data_file, ensure_data_dir

MAX_MEMORY = 15  # Количество запоминаемых реплик

_MEMORY_LOCK = threading.RLock()
_CHARACTERS_LOCK = threading.RLock()


# ----------------------------------------------------------------- утилиты
def _read_json(path, default):
    """Читает JSON или возвращает default. Битый файл откладывает в .corrupt-*."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        _quarantine(path)
        return default


def _quarantine(path):
    """Переименовывает битый файл, чтобы сохранить данные для разбора."""
    try:
        if os.path.exists(path):
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
    except OSError:
        pass


def _write_json_atomic(path, data):
    """Атомарно пишет JSON: сначала во временный файл, потом os.replace."""
    ensure_data_dir()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def memory_path():
    return data_file("memory.json")


def characters_path():
    return data_file("characters.json")


# ------------------------------------------------------------ память диалога
def load_memory():
    """Возвращает список реплик [{'original': ..., 'translated': ...}, ...]."""
    with _MEMORY_LOCK:
        data = _read_json(memory_path(), [])
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and "original" in item:
                result.append(item)
        return result[-MAX_MEMORY:]


def save_memory(original_text, translated_text):
    """Дописывает реплику в память (атомарно, под блокировкой)."""
    with _MEMORY_LOCK:
        memory = load_memory()
        memory.append({
            "original": original_text,
            "translated": translated_text,
        })
        if len(memory) > MAX_MEMORY:
            memory = memory[-MAX_MEMORY:]
        return _write_json_atomic(memory_path(), memory)


def clear_memory():
    """Полностью очищает память диалога."""
    with _MEMORY_LOCK:
        return _write_json_atomic(memory_path(), [])


def format_memory_for_prompt():
    memory = load_memory()
    if not memory:
        return "No previous context."

    context = "Previous dialogue (for context):\n"
    for item in memory:
        context += f"EN: {item.get('original', '')}\nRU: {item.get('translated', '')}\n"
    return context


# ------------------------------------------------------------- лор персонажей
def load_characters():
    with _CHARACTERS_LOCK:
        data = _read_json(characters_path(), {})
        return data if isinstance(data, dict) else {}


def save_character(name, description):
    """Сохраняет описание персонажа, если его ещё нет."""
    if not name or not description:
        return False
    with _CHARACTERS_LOCK:
        chars = load_characters()
        if name in chars:
            return False
        chars[name] = description
        return _write_json_atomic(characters_path(), chars)


def format_characters_for_prompt():
    chars = load_characters()
    if not chars:
        return ""
    context = "Character Lore (use this to match their tone):\n"
    for name, desc in chars.items():
        context += f"- {name}: {desc}\n"
    return context
