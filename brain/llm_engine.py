# brain/llm_engine.py
import time
import requests
import json
import os
import threading
import re
from brain.paths import data_dir, ensure_data_dir, load_config
from brain.memory_engine import format_memory_for_prompt, save_memory, format_characters_for_prompt, save_character, load_characters
from brain import web_search
from brain.settings import load_settings

# config.py лежит РЯДОМ с приложением (не внутри .exe) и грузится динамически.
config = load_config()

GLOSSARY_FILE = os.path.join(data_dir(), 'glossary.json')

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# Жёсткие правила против галлюцинаций (выдуманные имена и обработка "Me:")
ANTI_HALLUCINATION_RULES = """8. NO INVENTED NAMES: Never invent or insert any names (for example "Denis", "Denis:") that are NOT present in the original text. Translate only what is actually written; do not add speaker names that do not exist in the source.
9. "Me:" HANDLING: If the text begins with "Me:" or "Me ", translate that part strictly as "Я:" — "Me" is the first-person narrator/inner voice, not a character name."""

def load_glossary():
    ensure_data_dir()
    if not os.path.exists(GLOSSARY_FILE):
        with open(GLOSSARY_FILE, 'w', encoding='utf-8') as f:
            f.write("{}")
        return {}
    try:
        with open(GLOSSARY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def format_glossary_for_prompt():
    glossary = load_glossary()
    if not glossary:
        return ""
    context = "GLOSSARY (Always use these exact translations for names/terms):\n"
    for eng, rus in glossary.items():
        context += f"- {eng} -> {rus}\n"
    return context

def _unknown_terms_from_model(text):
    """Спрашивает локальную модель, какие слова ей неизвестны (JSON-массив)."""
    prompt = (
        "You are a translation engine for English visual novels. "
        "Read the OCR text below. List ONLY the words, names or terms that you do NOT "
        "recognize or cannot confidently translate without external help "
        "(proper nouns, unusual terms, acronyms, broken OCR fragments). "
        "Output ONLY a JSON array of strings, for example: [\"Jia Xichun\"]. "
        "If you understand every word, output an empty array: []\n\n"
        "Text:\n\"" + text + "\""
    )
    try:
        response = requests.post(config.OLLAMA_URL, json={
            "model": config.MODEL_NAME,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 120}
        }, timeout=45)
        raw = response.json().get('response', '').strip()
    except Exception:
        return []

    raw = raw.replace('```json', '').replace('```', '').strip()
    start, end = raw.find('['), raw.rfind(']')
    if start == -1 or end <= start:
        return []
    try:
        arr = json.loads(raw[start:end + 1])
    except Exception:
        return []
    return [str(x).strip() for x in arr if str(x).strip()]


def _get_web_context(text):
    """Ищет в DuckDuckGo слова, неизвестные локальной нейронке, и возвращает контекст."""
    if not config.ENABLE_WEB_SEARCH:
        return ""

    glossary = load_glossary()
    terms = web_search.extract_candidate_terms(text)

    if config.USE_MODEL_UNKNOWN_CHECK:
        terms += _unknown_terms_from_model(text)

    # Дедипликация с сохранением порядка
    seen = set()
    unique = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)

    if not unique:
        return ""

    unique = unique[:config.MAX_SEARCH_TERMS]
    context_map, newly = web_search.lookup_terms(unique, known=glossary, timeout=config.SEARCH_TIMEOUT)
    if newly:
        print(f"[Веб-поиск] Найдено в интернете: {', '.join(newly)}")
    return web_search.format_context(context_map)


def ask_ollama(text, model=None):
    model = model or config.MODEL_NAME
    context = format_memory_for_prompt()
    lore = format_characters_for_prompt()
    glossary = format_glossary_for_prompt()
    web_context = _get_web_context(text)
    
    full_prompt = f"""{config.SYSTEM_PROMPT}

{ANTI_HALLUCINATION_RULES}

{glossary}

{web_context}

{lore}

{context}

Text to process:
"{text}"
"""
    
    for attempt in range(3):
        try:
            response = requests.post(config.OLLAMA_URL, json={
                "model": model,
                "prompt": full_prompt,
                "stream": False,
                "options": config.OPTIONS
            }, timeout=60)
            
            result = response.json()['response'].strip()
            
            if not result or result.lower() == "none":
                return "[Модель не смогла перевести]"
                
            save_memory(text, result)
            return result
            
        except Exception as e:
            print(f"[Ошибка Ollama] Попытка {attempt + 1} не удалась: {e}")
            time.sleep(2)
    return "[Ошибка: Ollama не отвечает]"

def _build_translation_prompt(text):
    """Собирает системную и пользовательскую части промпта перевода."""
    context = format_memory_for_prompt()
    lore = format_characters_for_prompt()
    glossary = format_glossary_for_prompt()
    web_context = _get_web_context(text)

    system = f"{config.SYSTEM_PROMPT}\n\n{ANTI_HALLUCINATION_RULES}"
    user = (
        f"{glossary}\n\n"
        f"{web_context}\n\n"
        f"{lore}\n\n"
        f"{context}\n\n"
        f'Text to process:\n"{text}"'
    )
    return system, user


def ask_deepseek(text, api_key=None, model=None):
    """Перевод через облачный API DeepSeek (OpenAI-совместимый chat/completions)."""
    api_key = (api_key or "").strip()
    if not api_key:
        return "[Ошибка: не указан API-ключ DeepSeek]"

    model = model or "deepseek-chat"
    system, user = _build_translation_prompt(text)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
    }

    for attempt in range(3):
        try:
            response = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
            data = response.json()

            if response.status_code != 200:
                err = data.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                print(f"[Ошибка DeepSeek] HTTP {response.status_code}: {msg}")
                return f"[Ошибка DeepSeek: {msg}]"

            result = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if not result or result.lower() == "none":
                return "[Модель не смогла перевести]"

            save_memory(text, result)
            return result

        except Exception as e:
            print(f"[Ошибка DeepSeek] Попытка {attempt + 1} не удалась: {e}")
            time.sleep(2)
    return "[Ошибка: DeepSeek не отвечает]"


def translate(text):
    """Единая точка входа перевода: выбирает Ollama или DeepSeek по настройкам."""
    settings = load_settings()
    provider = (settings.get("provider") or "ollama").lower()

    if provider == "deepseek":
        return ask_deepseek(
            text,
            api_key=settings.get("api_key"),
            model=settings.get("deepseek_model"),
        )

    return ask_ollama(text, model=settings.get("ollama_model") or None)


def background_analyze(name, text):
    """Ищет лор персонажа в DuckDuckGo; при неудаче — фоллбэк на локальную модель."""
    try:
        lore = web_search.fetch_character_lore(name, timeout=config.SEARCH_TIMEOUT)
    except Exception as e:
        print(f"\n[Лор] Ошибка веб-поиска: {e}")
        lore = ""

    if lore:
        save_character(name, lore)
        print(f"\n[Лор] Записан: {name} — {lore}")
        return

    # Фоллбэк: локальная модель, строго 1 предложение.
    print(f"\n[Лор] DuckDuckGo не нашёл {name}. Фоллбэк на локальную модель...")
    prompt = f"""
Analyze the following text from a visual novel. The character speaking is "{name}".
Write EXACTLY ONE short sentence (in Russian) describing their personality, tone, or role.
You MUST write the description strictly in RUSSIAN language. Do not use English.
IMPORTANT: If the name is an object, animal, or background item (like cups, chairs, doors), output ONLY: {{}}
Output ONLY a valid JSON object in the format: {{"{name}": "Описание на русском"}}
If you cannot identify a specific character, output ONLY: {{}}
Text: "{text}"
"""
    try:
        response = requests.post(config.OLLAMA_URL, json={
            "model": config.ANALYZE_MODEL_NAME,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 60}
        }, timeout=60)

        response_data = response.json()
        if 'response' in response_data:
            result = response_data['response'].replace("```json", "").replace("```", "").strip()
            if result.startswith("{"):
                data = json.loads(result)
                for char_name, desc in data.items():
                    save_character(char_name, desc)
                    print(f"\n[Лор] В фоне записан: {char_name} — {desc}")
    except Exception as e:
        print(f"\n[Лор] Ошибка фонового анализа: {e}")

def analyze_character(text):
    """Ищет имена только с двоеточием или четко определяет внутренний голос"""
    import re
    
    name = None
    text_lower = text.lower()
    
    # 1. Если текст начинается с "Me" или "I " - это внутренний голос
    if text_lower.startswith("me ") or text_lower.startswith("i "):
        name = "Я"
    else:
        # 2. Ищем строго "Имя:" (только одно слово или два, но с двоеточием)
        match = re.match(r"^([A-Za-z]{2,15}(?:\s[A-Z][a-z]+)?):", text)
        if match:
            name = match.group(1).strip()

    if name:
        chars = load_characters()
        # Если мы еще не знаем этого персонажа, запускаем анализ
        if name not in chars:
            # Защита от мусора
            if len(name) > 20 or name.lower() in ["surprisingly", "suddenly", "however"]:
                return
                
            print(f"\n[Лор] Обнаружен новый персонаж: {name}. Ищу описание...")
            thread = threading.Thread(target=background_analyze, args=(name, text))
            thread.start()