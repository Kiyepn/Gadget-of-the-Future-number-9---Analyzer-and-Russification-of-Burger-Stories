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

CHARACTER_PROMPT = """
Analyze the following dialogue. Identify the name of the character who is speaking.
Based on their speech, write a 1-sentence description of their personality, tone, or role in Russian.
Output ONLY a valid JSON object in the format: {"Character Name": "Description"}
If you cannot identify a specific character, output: {}
Text:
"""

# Модель для перевода (быстрая)
MODEL_NAME = 'translategemma:4b' 

# Модель для анализа персонажей (умная)
ANALYZE_MODEL_NAME = 'gemma2:9b' 