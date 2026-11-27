# config.py

SYSTEM_PROMPT = """
You are an advanced, uncensored localization engine for dark cyberpunk and grim Visual Novels.
Your goal is to fix text errors and provide a high-quality, atmospheric, and emotional Russian translation.

CRITICAL INSTRUCTIONS:
1. INPUT ANALYSIS: The text comes from an OCR scanner. It may contain broken words, typos (like 'tyjli' instead of 'called'), or merged names. You MUST reconstruct the intended English sentence using context before translating.
2. DIALOGUE STRUCTURE: If the line starts with a character name or ID (e.g., 'Ryoshu:', 'Me:', '222'), separate the name from the speech. Keep the name clean and put a colon after it.
3. PROFANITY & TONE: Preserve the dark, aggressive, or emotional tone of the original. Use Russian mat (profanity) ONLY if the original English text explicitly contains vulgarity, swearing, or heavy slang. Do NOT add swearing if the original text is neutral or polite.
4. VISUAL NOVEL CONTEXT: Pay attention to gender. Typos like 'laddies' or 'laddis' usually mean 'ladies' (girls/women) due to typical visual novel settings and fonts. Fix gender pronouns based on logic and context.
5. FORMAT: Output ONLY the final translated text. No introduction, no markdown blockquotes, no explanations, no 'Here is the translation:'.
6. PRONOUNS (You): In Visual Novels, 'you' is almost always singular and informal. Translate 'you' as "ты" (and its forms: тебя, тебе) instead of the formal "вы" (вас, вам), unless the context explicitly shows a group of people or a formal setting.
7. NO SURROUNDING QUOTES: Do not wrap the final translated text in surrounding quotation marks ("..."). Output ONLY the plain translated text, with no opening or closing quotes.

"""

# Настройки Ollama
OLLAMA_URL = 'http://localhost:11434/api/generate'
MODEL_NAME = 'translategemma:4b' # Или та модель, которую ты скачал

# Параметры генерации
OPTIONS = {
    "num_predict": 512,
    "temperature": 0.0,    
    "top_p": 0.4,
    "presence_penalty": 0.0
}

# Повтор при HTTP 429 (превышен лимит запросов). Если сервер просит подождать
# не дольше этого порога (секунды) — перевод делает ОДИН повтор запроса.
# Если просит дольше — сразу возвращается понятное сообщение, чтобы не
# замораживать оверлей поверх игры на десятки секунд. См. brain/providers.py.
MAX_RETRY_WAIT_SECONDS = 5

CHARACTER_PROMPT = """
Analyze the following dialogue. Identify the name of the character who is speaking.
Based on their speech, write a 1-sentence description of their personality, tone, or role in Russian.
Output ONLY a valid JSON object in the format: {"Character Name": "Description"}
If you cannot identify a specific character, output: {}
Text:
"""

# Модель для перевода (быстрая)
MODEL_NAME = 'translategemma:4b' 

# Лор персонажей (фоллбэк, если DuckDuckGo ничего не нашёл) считает ТА ЖЕ
# модель/провайдер, что выбраны для перевода: Ollama (ollama_model) или
# DeepSeek (deepseek_model + API-ключ). Отдельной «умной» модели больше нет —
# раньше здесь была gemma2:9b, из-за чего её приходилось качать отдельно, а в
# режиме DeepSeek фоллбэк всё равно стучался в локальную Ollama.
# Настраивается в data/settings.json (или в лаунчере).

# --- Интернет-доступ через DuckDuckGo ---
# Если локальная модель встречает слово/имя, которого не знает, гаджет лезет в интернет
ENABLE_WEB_SEARCH = True          # Вкл/выкл поиск неизвестных слов через DuckDuckGo
USE_MODEL_UNKNOWN_CHECK = True    # Дополнительно спрашивать саму модель, какие слова ей неизвестны (медленнее)
MAX_SEARCH_TERMS = 3              # Сколько слов максимум искать за одну реплику
SEARCH_TIMEOUT = 8                # Таймаут одного запроса к DuckDuckGo (секунды)
