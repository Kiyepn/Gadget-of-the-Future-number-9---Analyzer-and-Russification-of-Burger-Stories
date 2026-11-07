# brain/llm_engine.py
import time
import requests
import config
import json
import os
import threading
import re
from brain.memory_engine import format_memory_for_prompt, save_memory, format_characters_for_prompt, save_character, load_characters

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

def ask_ollama(text):
    context = format_memory_for_prompt()
    lore = format_characters_for_prompt()
    glossary = format_glossary_for_prompt()
    
    full_prompt = f"""{config.SYSTEM_PROMPT}

{glossary}

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