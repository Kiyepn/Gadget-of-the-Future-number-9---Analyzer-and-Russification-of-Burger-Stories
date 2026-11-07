# brain/llm_engine.py
import time
import requests
import config
import json
import os
import threading
import re
from brain.memory_engine import format_memory_for_prompt, save_memory, format_characters_for_prompt, save_character, load_characters
from brain import web_search

GLOSSARY_FILE = os.path.join('data', 'glossary.json')

def load_glossary():
    if not os.path.exists('data'):
        os.makedirs('data')
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


def ask_ollama(text):
    context = format_memory_for_prompt()
    lore = format_characters_for_prompt()
    glossary = format_glossary_for_prompt()
    web_context = _get_web_context(text)
    
    full_prompt = f"""{config.SYSTEM_PROMPT}

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
                "model": config.MODEL_NAME, 
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

def background_analyze(name, text):
    """Фоновая задача, которая стучится в gemma2 и не тормозит игру"""
    prompt = f"""
Analyze the following text from a visual novel. The character speaking is "{name}".
Write a very short description (1 sentence) of their personality, tone, or role.
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
            "options": {"temperature": 0.1, "num_predict": 100}
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
                
            print(f"\n[Лор] Обнаружен новый персонаж: {name}. Запускаю анализ...")
            thread = threading.Thread(target=background_analyze, args=(name, text))
            thread.start()