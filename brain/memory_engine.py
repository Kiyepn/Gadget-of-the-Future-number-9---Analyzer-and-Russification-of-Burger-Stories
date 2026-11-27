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

В 1.6.0 (блок 2) characters.json хранит не строку, а карточку персонажа:
``{"gender", "pronouns", "address", "manner", "description"}``. Старый формат
(строка) читается как карточка с этим описанием, миграция файла — отдельная
функция migrate_characters_file() с бэкапом.
"""
import json
import os
import shutil
import threading
import time

from brain.paths import data_file, ensure_data_dir

MAX_MEMORY = 15  # Количество запоминаемых реплик

_MEMORY_LOCK = threading.RLock()
_CHARACTERS_LOCK = threading.RLock()

#: Карточка персонажа: пол, местоимения, обращение к собеседнику, манера, описание.
#: Используется как набор значений по умолчанию для старых записей и для
#: персонажей, которых модель ещё не видела.
DEFAULT_CHARACTER_CARD = {
    "gender": "unknown",
    "pronouns": "they/them",
    "address": "ty",
    "manner": "",
    "description": "",
}

_GENDERS = ("male", "female", "unknown")
_ADDRESSES = ("ty", "vy")
_PRONOUNS = ("he/him", "she/her", "they/them")
_PRONOUN_BY_GENDER = {
    "male": "he/him",
    "female": "she/her",
    "unknown": "they/them",
}



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
def normalize_character_card(value, description=None):
    """Приводит запись персонажа к карточке.

    Понимает оба формата: старую строку (она становится ``description``) и уже
    готовый словарь. Неизвестные поля сохраняются, чтобы новые поля не терялись
    при чтении-записи файла.
    """
    card = dict(DEFAULT_CHARACTER_CARD)
    if isinstance(value, dict):
        card.update({key: item for key, item in value.items() if item is not None})
    elif value is not None:
        card["description"] = str(value)

    if description is not None:
        card["description"] = str(description)

    gender = str(card.get("gender") or "").strip().lower()
    card["gender"] = gender if gender in _GENDERS else "unknown"

    # Местоимения: явно заданные моделью уважаем, иначе выводим их из пола.
    # (Раньше здесь дефолт "they/them" перебивал вывод he/him и she/her.)
    given_pronouns = ""
    if isinstance(value, dict):
        given_pronouns = str(value.get("pronouns") or "").strip().lower()
    pronouns = given_pronouns if given_pronouns in _PRONOUNS else ""
    if not pronouns:
        pronouns = _PRONOUN_BY_GENDER[card["gender"]]
    card["pronouns"] = pronouns

    address = str(card.get("address") or "").strip().lower()
    card["address"] = address if address in _ADDRESSES else "ty"

    card["manner"] = str(card.get("manner") or "").strip()
    card["description"] = str(card.get("description") or "").strip()
    return card


def _normalize_all(data):
    """Нормализует словарь персонажей. Возвращает (карточки, сколько старых)."""
    result = {}
    legacy = 0
    for name, value in (data or {}).items():
        name = str(name).strip()
        if not name:
            continue
        if not isinstance(value, dict):
            legacy += 1
        result[name] = normalize_character_card(value)
    return result, legacy


def load_characters():
    """Возвращает {имя: карточка}. Старый формат мигрируется при чтении."""
    with _CHARACTERS_LOCK:
        data = _read_json(characters_path(), {})
        if not isinstance(data, dict):
            return {}
        cards, _ = _normalize_all(data)
        return cards


def _backup_characters_file(path=None):
    """Копия characters.json перед перезаписью миграции. None, если не вышло."""
    path = path or characters_path()
    try:
        if not os.path.exists(path):
            return None
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


def migrate_characters_file():
    """Переводит characters.json на формат карточек.

    Возвращает ``(сколько_записей_мигрировано, путь_бэкапа_или_None)``.
    Старое описание не теряется — оно становится полем ``description``.
    Повторный вызов ничего не делает (идемпотентно).
    """
    with _CHARACTERS_LOCK:
        path = characters_path()
        data = _read_json(path, {})
        if not isinstance(data, dict) or not data:
            return 0, None

        cards, legacy = _normalize_all(data)
        if not legacy:
            return 0, None

        backup = _backup_characters_file(path)
        if not _write_json_atomic(path, cards):
            return 0, backup
        return legacy, backup


def save_character(name, description=None, card=None):
    """Сохраняет карточку персонажа, если его ещё нет.

    ``description`` — прежний интерфейс (краткое описание), ``card`` — словарь
    с полями gender/pronouns/address/manner/description. Если переданы оба,
    описание из аргумента имеет приоритет.
    """
    name = str(name or "").strip()
    if not name:
        return False

    merged = dict(card) if isinstance(card, dict) else {}
    if description is not None:
        merged["description"] = description
    normalized = normalize_character_card(merged)
    if not normalized["description"]:
        return False

    with _CHARACTERS_LOCK:
        path = characters_path()
        raw = _read_json(path, {})
        if not isinstance(raw, dict):
            raw = {}

        cards, legacy = _normalize_all(raw)
        if name in cards:
            return False
        if legacy:
            # В файле ещё старый формат: сохраняем копию перед перезаписью,
            # чтобы данные пользователя нельзя было потерять.
            _backup_characters_file(path)

        cards[name] = normalized
        return _write_json_atomic(path, cards)


def get_character_card(name, characters=None):
    """Карточка персонажа или значения по умолчанию, если он неизвестен."""
    name = str(name or "").strip()
    cards = characters if characters is not None else load_characters()
    if not name:
        return dict(DEFAULT_CHARACTER_CARD)

    if name in cards:
        return dict(cards[name])

    # OCR иногда меняет регистр — сравниваем без него.
    folded = name.casefold()
    for known, card in cards.items():
        if str(known).casefold() == folded:
            return dict(card)
    return dict(DEFAULT_CHARACTER_CARD)


def format_character_line(name, card):
    """Одна строка лора для промпта: пол, местоимения, обращение, манера."""
    card = normalize_character_card(card)
    address = "ты" if card["address"] == "ty" else "вы"
    parts = [
        f"gender={card['gender']}",
        f"pronouns={card['pronouns']}",
        f"address={card['address']} ({address})",
    ]
    if card["manner"]:
        parts.append(f"manner={card['manner']}")
    line = f"- {name}: " + ", ".join(parts)
    if card["description"]:
        line += f"; {card['description']}"
    return line + "\n"


def format_characters_for_prompt(characters=None):
    chars = characters if characters is not None else load_characters()
    if not chars:
        return ""
    context = "Character Lore (use this to match their tone):\n"
    for name, card in chars.items():
        context += format_character_line(name, card)
    return context
