# brain/memory_engine.py
import json
import os

MEMORY_FILE = os.path.join('data', 'memory.json')
CHARACTERS_FILE = os.path.join('data', 'characters.json')
MAX_MEMORY = 15 # Количество запоминаемых реплик

# --- РАБОТА С ПАМЯТЬЮ ДИАЛОГОВ ---

def load_memory():
    if not os.path.exists('data'):
        os.makedirs('data')
        
    if not os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            f.write("[]")
        return []
        
    try:
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_memory(original_text, translated_text):
    memory = load_memory()
    memory.append({
        "original": original_text,
        "translated": translated_text
    })
    
    if len(memory) > MAX_MEMORY:
        memory = memory[-MAX_MEMORY:]
        
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def format_memory_for_prompt():
    memory = load_memory()
    if not memory:
        return "No previous context."
    
    context = "Previous dialogue (for context):\n"
    for item in memory:
        context += f"EN: {item['original']}\nRU: {item['translated']}\n"
    return context

# --- РАБОТА С ПЕРСОНАЖАМИ (ЛОР) ---

def load_characters():
    if not os.path.exists('data'):
        os.makedirs('data')
        
    if not os.path.exists(CHARACTERS_FILE):
        with open(CHARACTERS_FILE, 'w', encoding='utf-8') as f:
            f.write("{}")
        return {}
        
    try:
        with open(CHARACTERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_character(name, description):
    chars = load_characters()
    if name not in chars:
        chars[name] = description
        with open(CHARACTERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(chars, f, ensure_ascii=False, indent=2)

def format_characters_for_prompt():
    chars = load_characters()
    if not chars:
        return ""
    context = "Character Lore (use this to match their tone):\n"
    for name, desc in chars.items():
        context += f"- {name}: {desc}\n"
    return context