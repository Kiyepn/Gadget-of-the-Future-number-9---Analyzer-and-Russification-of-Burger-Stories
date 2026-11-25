# brain/llm_engine.py
import logging
import time
import requests
import json
import os
import threading
import re
from brain.paths import config_path as app_config_path
from brain.paths import data_file, ensure_data_dir, load_config
from brain.memory_engine import format_memory_for_prompt, save_memory, format_characters_for_prompt, save_character, load_characters
from brain import web_search
from brain.settings import load_settings

log = logging.getLogger(__name__)

# config.py лежит РЯДОМ с приложением (не внутри .exe) и грузится динамически.
config = load_config()


def glossary_path():
    """Путь к глоссарию (папку data выбирает brain.paths, возможно LocalAppData)."""
    return data_file('glossary.json')


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# Жёсткие правила против галлюцинаций (выдуманные имена и обработка "Me:")
ANTI_HALLUCINATION_RULES = """8. NO INVENTED NAMES: Never invent or insert any names (for example "Denis", "Denis:") that are NOT present in the original text. Translate only what is actually written; do not add speaker names that do not exist in the source.
9. "Me:" HANDLING: If the text begins with "Me:" or "Me ", translate that part strictly as "Я:" — "Me" is the first-person narrator/inner voice, not a character name."""

def load_glossary():
    ensure_data_dir()
    path = glossary_path()
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write("{}")
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
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
        log.info(f"[Веб-поиск] Найдено в интернете: {', '.join(newly)}")
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
            log.warning(f"[Ошибка Ollama] Попытка {attempt + 1} не удалась: {e}")
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
                log.error(f"[Ошибка DeepSeek] HTTP {response.status_code}: {msg}")
                if response.status_code == 429:
                    return (
                        "[Ошибка DeepSeek: превышен лимит запросов (429). "
                        "Подожди немного и повтори.]"
                    )
                return f"[Ошибка DeepSeek: {msg}]"

            result = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if not result or result.lower() == "none":
                return "[Модель не смогла перевести]"

            save_memory(text, result)
            return result

        except Exception as e:
            log.warning(f"[Ошибка DeepSeek] Попытка {attempt + 1} не удалась: {e}")
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


# ============ лор персонажей ============
# Раньше фоллбэк лора жёстко считался на gemma2:9b (config.ANALYZE_MODEL_NAME).
# Из-за этого требовалась модель, которой нет в лаунчере, а в режиме DeepSeek
# запрос всё равно уходил в локальную Ollama. Теперь лор считает та же модель,
# что выбрана для перевода.

_LEGACY_ANALYZE_WARNED = False
_LEGACY_CONFIG_RE = re.compile(r"^[ \t]*ANALYZE_MODEL_NAME[ \t]*=.*(?:\r?\n)?", re.MULTILINE)

# Защита от повторного анализа одного и того же персонажа:
# * _LORE_INFLIGHT — имя уже в работе (два кадра подряд до записи в characters.json);
# * _LORE_LAST_ATTEMPT — недавняя неудачная попытка (чтобы не дёргать модель
#   каждый кадр, если лор так и не нашёлся).
_LORE_INFLIGHT = set()
_LORE_LAST_ATTEMPT = {}
_LORE_LOCK = threading.Lock()
_LORE_RETRY_SECONDS = 600.0


def _migrate_legacy_config(path=None):
    """Убирает устаревшую ANALYZE_MODEL_NAME из config.py рядом с приложением.

    Нужно тем, кто обновил программу, но оставил старый config.py: иначе
    предупреждение «константа больше не используется» появлялось бы в журнале
    при каждом запуске. Удаляем только строку с этой константой, с бэкапом и
    проверкой компиляции: при любой проблеме файл не трогаем.

    Возвращает True, если файл действительно поправлен.
    """
    path = path or app_config_path()
    try:
        if not path or not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as stream:
            original = stream.read()
        if "ANALYZE_MODEL_NAME" not in original:
            return False

        updated = _LEGACY_CONFIG_RE.sub("", original)
        if updated == original:
            return False

        compile(updated, path, "exec")  # проверка, что config.py остался валидным

        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(backup, "w", encoding="utf-8") as stream:
            stream.write(original)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(updated)
    except Exception as exc:
        log.warning(
            "[Лор] Не удалось убрать устаревшую ANALYZE_MODEL_NAME из config.py: %s. "
            "Её можно удалить вручную — она больше не используется.",
            exc,
        )
        return False

    log.info(
        "[Лор] Убрал устаревшую константу ANALYZE_MODEL_NAME из config.py "
        "(бэкап: %s). Лор считает модель, выбранная для перевода.",
        os.path.basename(backup),
    )
    return True


def _lore_should_skip(name):
    """True, если персонажа уже анализируют или анализировали только что."""
    now = time.monotonic()
    with _LORE_LOCK:
        if name in _LORE_INFLIGHT:
            log.debug("[Лор] «%s» уже анализируется — новый запрос не нужен.", name)
            return True
        last = _LORE_LAST_ATTEMPT.get(name)
        if last is not None and (now - last) < _LORE_RETRY_SECONDS:
            log.debug(
                "[Лор] «%s» проверяли %.0f с назад — повторю позже.",
                name,
                now - last,
            )
            return True
    return False


def _lore_mark_inflight(name):
    with _LORE_LOCK:
        _LORE_INFLIGHT.add(name)


def _lore_release(name):
    with _LORE_LOCK:
        _LORE_INFLIGHT.discard(name)
        _LORE_LAST_ATTEMPT[name] = time.monotonic()


def _ollama_base_url():
    """База Ollama из config.OLLAMA_URL (…/api/generate -> http://host:port)."""
    url = (getattr(config, "OLLAMA_URL", "") or "http://localhost:11434/api/generate")
    return url.split("/api/")[0].rstrip("/")


def _normalize_model_name(name):
    """«qwen2.5» -> («qwen2.5», «latest»); нужно для сравнения тегов Ollama."""
    raw = (name or "").strip().lower()
    if not raw:
        return "", ""
    if ":" in raw:
        base, tag = raw.rsplit(":", 1)
        return base, (tag or "latest")
    return raw, "latest"


def _same_model(left, right):
    return _normalize_model_name(left) == _normalize_model_name(right)


def lore_model_config(settings=None):
    """Модель и провайдер для лора = те же, что выбраны для перевода.

    Возвращает словарь {"provider", "model", "api_key"}. Ключ нужен только
    для DeepSeek.
    """
    global _LEGACY_ANALYZE_WARNED

    if not _LEGACY_ANALYZE_WARNED:
        _LEGACY_ANALYZE_WARNED = True
        # Сначала пробуем починить старый config.py, а не просто предупреждать.
        if not _migrate_legacy_config():
            legacy = getattr(config, "ANALYZE_MODEL_NAME", None)
            if legacy:
                log.info(
                    "[Лор] config.ANALYZE_MODEL_NAME (%s) больше не используется: "
                    "лор считает модель, выбранная для перевода.",
                    legacy,
                )

    settings = settings or load_settings()
    provider = str(settings.get("provider") or "ollama").strip().lower()
    if provider == "deepseek":
        model = str(settings.get("deepseek_model") or "").strip() or "deepseek-chat"
    else:
        provider = "ollama"
        model = str(settings.get("ollama_model") or "").strip() or config.MODEL_NAME

    return {
        "provider": provider,
        "model": model,
        "api_key": str(settings.get("api_key") or "").strip(),
    }


def _ollama_model_ready(model, timeout=2.5):
    """Проверяет, что Ollama запущена и нужная модель скачана.

    Возвращает (True, "") или (False, причина) — чтобы в журнале была понятная
    причина, а не голый таймаут.
    """
    try:
        response = requests.get(f"{_ollama_base_url()}/api/tags", timeout=timeout)
        if response.status_code != 200:
            return False, f"Ollama ответила HTTP {response.status_code}"
        payload = response.json() or {}
        names = [
            str(item.get("name") or item.get("model") or "")
            for item in (payload.get("models") or [])
        ]
    except Exception as exc:
        return False, f"Ollama недоступна ({exc})"

    if not any(_same_model(name, model) for name in names):
        # Модель может быть не скачана — это не ошибка сети, но лор не получится.
        return False, f"модель «{model}» не найдена в Ollama (скачайте её в лаунчере)"
    return True, ""


def _lore_completion(prompt, model_config, timeout=60):
    """Запрос к выбранной модели. Возвращает текст ответа или None.

    None означает «провайдер недоступен/не настроен» — в этом случае лор
    остаётся пустым, а приложение продолжает работать.
    """
    provider = (model_config or {}).get("provider") or "ollama"
    model = (model_config or {}).get("model") or config.MODEL_NAME

    if provider == "deepseek":
        api_key = (model_config or {}).get("api_key") or ""
        if not api_key:
            log.warning("[Лор] Выбран DeepSeek, но API-ключ не задан — лор не будет сохранён.")
            return None
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты описываешь персонажей визуальных новелл. "
                        "Отвечай только валидным JSON-объектом на русском языке."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 120,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            started = time.monotonic()
            response = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
            data = response.json()
            elapsed = time.monotonic() - started
            if response.status_code == 429:
                retry_after = ""
                try:
                    retry_after = str(response.headers.get("Retry-After") or "")
                except Exception:
                    retry_after = ""
                log.warning(
                    "[Лор] DeepSeek: превышен лимит запросов (429 Too Many Requests)%s "
                    "— лор не сохранён, попробую позже.",
                    f", сервер просит подождать {retry_after} с" if retry_after else "",
                )
                return None
            if response.status_code != 200:
                err = (data.get("error") or {}) if isinstance(data, dict) else {}
                message = err.get("message") if isinstance(err, dict) else str(err)
                log.warning("[Лор] DeepSeek ответил HTTP %s: %s", response.status_code, message)
                return None
            choices = data.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content", "")
            log.info("[Лор] Модель %s ответила за %.2f с.", model, elapsed)
            return content or None
        except Exception as exc:
            log.warning("[Лор] DeepSeek недоступен: %s", exc)
            return None

    ready, reason = _ollama_model_ready(model)
    if not ready:
        log.warning("[Лор] Локальная модель недоступна: %s — лор не будет сохранён.", reason)
        return None

    try:
        started = time.monotonic()
        response = requests.post(
            config.OLLAMA_URL,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 80},
            },
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        data = response.json() or {}
        if data.get("error"):
            log.warning("[Лор] Ollama вернула ошибку: %s", data["error"])
            return None
        log.info("[Лор] Модель %s ответила за %.2f с.", model, elapsed)
        return data.get("response") or None
    except Exception as exc:
        log.warning("[Лор] Ollama недоступна: %s", exc)
        return None


def _parse_lore_json(raw, expected_name=""):
    """Достаёт {имя: описание} из ответа модели (учитывает ```json-обёртку)."""
    if not raw:
        return {}
    cleaned = str(raw).replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    # Модели любят вернуть сам шаблон из промпта вместо описания.
    placeholders = {
        "описание на русском",
        "описание на русском.",
        "короткое описание на русском.",
        "краткое описание",
        "описание",
        "description",
        "описание персонажа",
    }
    # qwen2.5:3b иногда подмешивает китайские/японские символы в русский ответ —
    # такое описание лучше не сохранять, чем потом показывать в лоре.
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

    result = {}
    for char_name, desc in data.items():
        char_name = str(char_name).strip()
        desc = str(desc).strip()
        if not char_name or not desc:
            continue
        if desc in ("{}", "[]", "null", "None"):
            continue
        if desc.lower() in placeholders or len(desc) < 6:
            log.info("[Лор] Ответ модели похож на заглушку (%r) — пропускаю.", desc[:40])
            continue
        if cjk.search(desc):
            log.info("[Лор] В описании китайские/японские символы — пропускаю (%r).", desc[:40])
            continue
        result[char_name] = desc
    return result


def _build_lore_prompt(name, text):
    # Формулировка проверена на qwen2.5:3b: мягкий вариант («если не персонаж —
    # ответь {}») модель понимала слишком буквально и возвращала {} почти всегда,
    # а вариант с примером иногда просто копировал пример. Этот даёт описание.
    return f'''The line below is spoken by the character "{name}" in a visual novel.
Based only on this line, write ONE short sentence in RUSSIAN describing their personality, tone or role.
Reply with ONLY a JSON object, no explanations: {{"{name}": "описание на русском"}}
Answer {{}} ONLY if "{name}" is clearly not a person (an object, a number, a UI button or a sound).
Line: "{text}"'''


def background_analyze(name, text, model_config=None):
    """Ищет лор в DuckDuckGo; при неудаче — спрашивает модель перевода."""
    started = time.monotonic()
    config_used = dict(model_config or lore_model_config())

    try:
        try:
            lore = web_search.fetch_character_lore(name, timeout=config.SEARCH_TIMEOUT)
        except Exception as exc:
            log.warning("[Лор] Ошибка веб-поиска: %s", exc)
            lore = ""
        search_elapsed = time.monotonic() - started

        if lore:
            save_character(name, lore)
            log.info(
                "[Лор] Записан: %s — %s (DuckDuckGo, %.2f с)",
                name,
                lore,
                search_elapsed,
            )
            return

        log.info(
            "[Лор] DuckDuckGo не нашёл %s за %.2f с. Фоллбэк на %s (провайдер: %s).",
            name,
            search_elapsed,
            config_used.get("model"),
            config_used.get("provider"),
        )
        raw = _lore_completion(_build_lore_prompt(name, text), config_used)
        total_elapsed = time.monotonic() - started
        parsed = _parse_lore_json(raw, expected_name=name)
        if not parsed:
            log.info(
                "[Лор] Модель %s не дала описания для «%s» — пропускаю (всего %.2f с).",
                config_used.get("model"),
                name,
                total_elapsed,
            )
            return

        for char_name, desc in parsed.items():
            save_character(char_name, desc)
            log.info(
                "[Лор] Записан: %s — %s (модель %s, всего %.2f с)",
                char_name,
                desc,
                config_used.get("model"),
                total_elapsed,
            )
    finally:
        _lore_release(name)


def analyze_character(text, settings=None):
    """Ищет имена только с двоеточием или четко определяет внутренний голос"""
    import re

    name = None
    text_lower = text.lower()

    # 1. Если текст начинается с "Me" или "I " - это внутренний голос.
    #    "Me:" тоже внутренний голос: в промпте перевода он становится "Я:".
    if (
        text_lower.startswith("me ")
        or text_lower.startswith("me:")
        or text_lower.startswith("i ")
    ):
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

            # Не анализируем повторно: имя уже в работе или проверяли недавно.
            if _lore_should_skip(name):
                return

            # Модель для лора = модель перевода (см. lore_model_config).
            try:
                model_config = lore_model_config(settings)
            except Exception as exc:
                log.warning("[Лор] Не удалось определить модель для лора: %s", exc)
                model_config = None

            log.info(
                "[Лор] Обнаружен новый персонаж: %s. Ищу описание (модель: %s)...",
                name,
                (model_config or {}).get("model", "неизвестно"),
            )
            _lore_mark_inflight(name)
            try:
                thread = threading.Thread(
                    target=background_analyze, args=(name, text, model_config), daemon=True
                )
                thread.start()
            except Exception:
                _lore_release(name)
                raise