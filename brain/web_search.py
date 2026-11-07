# brain/web_search.py
"""Интернет-доступ через DuckDuckGo для локальной нейросети.

Когда локальная модель встречает неизвестное слово или имя, этот модуль
ищет его в DuckDuckGo и возвращает короткий контекст, который добавляется
в промпт перевода. Результаты кэшируются в data/web_cache.json, чтобы не
долбить интернет по одним и тем же именам.
"""
import html
import json
import os
import re
import time

import requests
from concurrent.futures import ThreadPoolExecutor

from brain.paths import data_dir, ensure_data_dir

CACHE_FILE = os.path.join(data_dir(), 'web_cache.json')

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
}

# Если сети нет — не стучимся в DuckDuckGo на каждый кадр, а делаем паузу.
_OFFLINE_UNTIL = 0.0
_OFFLINE_PAUSE = 60.0

# Частотные английские слова, которые НЕ считаем неизвестными терминами.
_COMMON_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'if', 'then', 'so', 'for', 'nor',
    'yet', 'as', 'at', 'of', 'in', 'on', 'to', 'from', 'by', 'with', 'about',
    'against', 'between', 'into', 'through', 'during', 'before', 'after',
    'above', 'below', 'up', 'down', 'out', 'off', 'over', 'under', 'again',
    'further', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all',
    'any', 'both', 'each', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'not', 'only', 'own', 'same', 'than', 'too', 'very', 'just', 'now',
    'is', 'am', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has',
    'had', 'do', 'does', 'did', 'will', 'would', 'shall', 'should', 'can',
    'could', 'may', 'might', 'must', 'i', 'you', 'your', 'yours', 'we', 'our',
    'ours', 'us', 'me', 'my', 'mine', 'he', 'him', 'his', 'she', 'her', 'hers',
    'it', 'its', 'they', 'them', 'their', 'theirs', 'this', 'that', 'these',
    'those', 'who', 'whom', 'whose', 'what', 'which', 'one', 'two', 'three',
    'first', 'last', 'next', 'time', 'day', 'thing', 'way', 'lot', 'bit',
    'still', 'back', 'good', 'bad', 'new', 'old', 'big', 'small', 'long',
    'short', 'able', 'also', 'always', 'never', 'ever', 'said', 'say', 'says',
    'like', 'know', 'think', 'want', 'see', 'look', 'come', 'go', 'get', 'got',
    'make', 'let', 'lets', 'right', 'ok', 'okay', 'oh', 'ah', 'uh', 'um',
    'hey', 'hmm', 'huh', 'yeah', 'nope', 'yes', 'please', 'thanks', 'thank',
    'sorry', 'sure', 'really', 'dont', 'cant', 'wont', 'im', 'ive', 'id',
    'ill', 'youre', 'youve', 'youd', 'well', 'scan', 'pay', 'code', 'payment',
    'complete', 'wait', 'minute', 'minutes', 'ago', 'course', 'everything',
    'something', 'nothing', 'anything', 'anyone', 'someone', 'everyone',
    'because', 'while', 'although', 'though', 'even', 'without', 'since',
    'until', 'later', 'early', 'soon', 'today', 'tomorrow', 'yesterday',
    'night', 'morning', 'evening', 'afternoon', 'tell', 'told', 'going',
    'gonna', 'wanna', 'gotta', 'else', 'much', 'many', 'little', 'great',
    'around', 'away', 'together', 'perhaps', 'maybe', 'probably', 'actually',
    'sometimes', 'usually', 'often', 'finally', 'suddenly', 'true', 'false',
}


def _normalize(word):
    return re.sub(r'[^a-z]', '', (word or '').lower())


def is_valid_search_term(term):
    """Пропускает к поиску только имена собственные и сложные термины/аббревиатуры.

    Отсекает: одиночные слова короче 4 букв, стандартные английские слова
    (артикли, предлоги, местоимения) и банальные фразы типа "Oh wait", "minutes ago".
    """
    t = (term or '').strip()
    if not t:
        return False

    words = t.split()
    norm_words = [_normalize(w) for w in words]

    # 1. Фразы целиком из частых/служебных слов — не ищем.
    if all(n in _COMMON_WORDS for n in norm_words):
        return False

    # 2. Одиночное слово: короче 4 букв — не ищем, и оно должно быть
    #    именем собственным (с заглавной буквы) или аббревиатурой (ВСЕ КАПСОМ).
    if len(words) == 1:
        if len(words[0]) < 4:
            return False
        if not words[0][0].isupper():
            return False
        return True

    # 3. Сложный термин ("N Corp", "Jia Xichun", "New York"):
    #    каждое слово должно начинаться с заглавной буквы.
    for w in words:
        if not w[0].isupper():
            return False
    return True


def _load_cache():
    ensure_data_dir()
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _clean(text):
    text = html.unescape(text or '')
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _instant_answer(query, timeout=8):
    """Бесплатный DuckDuckGo Instant Answer API (без ключа)."""
    try:
        resp = requests.get(
            'https://api.duckduckgo.com/',
            params={'q': query, 'format': 'json', 'no_html': 1, 'skip_disambig': 1},
            headers=_HEADERS,
            timeout=timeout,
        )
        data = resp.json()
        parts = []
        if data.get('AbstractText'):
            parts.append(data['AbstractText'])
        elif data.get('Abstract'):
            parts.append(data['Abstract'])
        for topic in data.get('RelatedTopics', [])[:3]:
            if isinstance(topic, dict) and topic.get('Text'):
                parts.append(_clean(topic['Text']))
        if data.get('Definition'):
            parts.append(data['Definition'])
        return _clean(' '.join(parts))
    except Exception:
        return ''


def _html_search(query, timeout=8):
    """Парсинг HTML-выдачи DuckDuckGo без BeautifulSoup (только requests + regex)."""
    try:
        resp = requests.get(
            'https://html.duckduckgo.com/html/',
            params={'q': query},
            headers=_HEADERS,
            timeout=timeout,
        )
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.S)
        parts = [_clean(s) for s in snippets[:3]]
        return ' '.join(p for p in parts if p)
    except Exception:
        return ''


def search_duckduckgo(query, timeout=8):
    """Возвращает короткий контекст по запросу из DuckDuckGo (или '')."""
    global _OFFLINE_UNTIL
    query = (query or '').strip()
    if not query:
        return ''
    if time.time() < _OFFLINE_UNTIL:
        return ''

    combined = _instant_answer(query, timeout=timeout)
    if len(combined) < 40:
        fallback = _html_search(query, timeout=timeout)
        combined = _clean((combined + ' ' + fallback).strip())

    if not combined:
        _OFFLINE_UNTIL = time.time() + _OFFLINE_PAUSE
    return combined[:600]


def extract_candidate_terms(text):
    """Достаёт из текста потенциально неизвестные слова: имена, названия, аббревиатуры."""
    if not text:
        return []

    words = re.findall(r"[A-Za-z][A-Za-z']*", text)
    raw_terms = []
    i = 0
    while i < len(words):
        w = words[i]
        if not w[0].isupper():
            i += 1
            continue
        seq = [w]
        j = i + 1
        while j < len(words):
            nxt = words[j]
            if nxt[0].isupper() and _normalize(nxt) not in _COMMON_WORDS:
                seq.append(nxt)
                j += 1
            else:
                break
        raw_terms.append(' '.join(seq))
        i = j

    final = []
    seen = set()
    for t in raw_terms:
        if not is_valid_search_term(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        final.append(t)

    return final


def lookup_terms(terms, known=None, timeout=8, max_workers=3):
    """Для каждого термина берёт кэш или ищет в DuckDuckGo.

    Возвращает (словарь термин -> контекст, список только что найденных терминов).
    """
    cache = _load_cache()
    known = {_normalize(k) for k in (known or {})}

    to_search = []
    result = {}
    for term in terms:
        # Жёсткий фильтр: не ищем мусор/обычные слова, даже если их вернула модель.
        if not is_valid_search_term(term):
            continue
        key = term.lower()
        if _normalize(term) in known:
            continue
        if key in cache and cache[key]:
            result[term] = cache[key]
        else:
            to_search.append(term)

    newly = []
    if to_search:
        def _run(t):
            return t, search_duckduckgo(t, timeout=timeout)

        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_search))) as ex:
            for term, ctx in ex.map(_run, to_search):
                if ctx:
                    cache[term.lower()] = ctx
                    result[term] = ctx
                    newly.append(term)

    _save_cache(cache)
    return result, newly


def format_context(term_context):
    if not term_context:
        return ''
    lines = ['WEB CONTEXT (found via DuckDuckGo; use it to translate unknown terms correctly):']
    for term, ctx in term_context.items():
        lines.append(f'- {term}: {ctx}')
    return '\n'.join(lines)

