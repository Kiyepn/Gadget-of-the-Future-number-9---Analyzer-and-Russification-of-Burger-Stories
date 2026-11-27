# brain/providers.py
"""Единая абстракция LLM-провайдеров (блок 1 релиза 1.6.0).

Зачем это нужно
---------------
До 1.6.0 у Ollama и DeepSeek были отдельные функции с разными форматами запроса
(нативный ``/api/generate`` против ``/chat/completions``), и в каждой заново
жили свои повторы, таймауты и разбор ошибок. Здесь все провайдеры общаются по
одному OpenAI-совместимому протоколу ``POST {base_url}/chat/completions``,
поэтому:

* обработка 429 / 401 / таймаутов и сетевых ошибок — в одном месте;
* логирование времени ответа и длин запроса/ответа — в одном месте;
* retry для 429 (с учётом ``Retry-After``) — в одном месте;
* новый провайдер = одна строка в ``PROVIDERS``.

Ollama используется через свой OpenAI-совместимый endpoint ``/v1`` (а не через
нативный ``/api/generate``). Адрес выводится из ``config.OLLAMA_URL``, поэтому
пользовательские настройки хоста и порта продолжают работать.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)

#: Облачный DeepSeek. Оба варианта (``/chat/completions`` и ``/v1/chat/completions``)
#: у DeepSeek рабочие, но ``/v1`` — это ровно то, что описано в их документации
#: как OpenAI-совместимый префикс.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

#: Запасной адрес Ollama, если config.py недоступен.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "translategemma:4b"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"

#: Запасной порог ожидания для 429, если в config.py нет MAX_RETRY_WAIT_SECONDS.
#: Сам Retry-After не угадывается: нет заголовка — нет и повтора.
DEFAULT_RETRY_WAIT_SECONDS = 5.0

#: Суффиксы, которые пользователь мог дописать в config.OLLAMA_URL.
_URL_SUFFIXES = (
    "/v1/chat/completions",
    "/api/generate",
    "/api/chat",
    "/chat/completions",
)

#: Человеческие имена провайдеров для сообщений в журнале и в оверлее.
_DISPLAY_NAMES = {
    "ollama": "Ollama",
    "deepseek": "DeepSeek",
}


# --------------------------------------------------------------------------- #
#  Ошибки: вызывающий код различает их, а не разбирает строки
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    """Базовая ошибка провайдера (некорректный ответ, пустой ``choices`` и т.п.)."""


class ProviderHTTPError(ProviderError):
    """HTTP-ответ не-200: несёт код и сообщение из тела ответа API.

    Нужен, чтобы в интерфейс попадало осмысленное «[Ошибка DeepSeek: <текст>]»,
    как это было в 1.5.2, а не голый ``requests.HTTPError``.
    """

    def __init__(self, provider: str, status_code: int, message: str = ""):
        self.provider = provider
        self.status_code = status_code
        self.message = message or f"HTTP {status_code}"
        super().__init__(self.message)


class RateLimitError(ProviderError):
    """HTTP 429 Too Many Requests.

    ``retry_after`` — сколько секунд просил подождать сервер, либо ``None``,
    если заголовка Retry-After не было (тогда задержку не выдумываем).
    """

    def __init__(self, provider: str, retry_after: float | None = None):
        self.provider = provider
        self.retry_after = None if retry_after is None else float(retry_after)
        if self.retry_after is None:
            detail = " (Retry-After не указан)"
        else:
            detail = f" (Retry-After: {self.retry_after:g} s)"
        super().__init__(f"HTTP 429 Too Many Requests{detail}")


# --------------------------------------------------------------------------- #
#  Доступ к config.py без циклического импорта
# --------------------------------------------------------------------------- #
#: Кэш config.py. ``False`` — «файл недоступен», чтобы не пытаться снова.
_APP_CONFIG = None


def _app_config():
    """Лениво загружает config.py из папки приложения (как brain.paths)."""
    global _APP_CONFIG
    if _APP_CONFIG is None:
        try:
            from brain.paths import load_config

            _APP_CONFIG = load_config()
        except Exception:  # config.py может отсутствовать в собранном .exe
            _APP_CONFIG = False
    return _APP_CONFIG or None


def _config_value(name: str, default):
    """Значение константы из config.py или ``default``, если её нет."""
    config = _app_config()
    if config is None:
        return default
    return getattr(config, name, default)


def max_retry_wait_seconds() -> float:
    """Порог ожидания для 429 (в секундах), настраивается в config.py.

    Если сервер просит подождать не дольше порога — запрос повторяется один раз.
    Если дольше — сразу возвращаем понятное сообщение, чтобы не замораживать
    перевод (и оверлей поверх игры) на десятки секунд.
    """
    raw = _config_value("MAX_RETRY_WAIT_SECONDS", DEFAULT_RETRY_WAIT_SECONDS)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_WAIT_SECONDS


# --------------------------------------------------------------------------- #
#  Адреса
# --------------------------------------------------------------------------- #
def _normalize_host(url: str) -> str:
    """Приводит любой вариант OLLAMA_URL к корню сервера без ``/v1`` и ``/api``."""
    value = str(url or "").strip().rstrip("/")
    for suffix in _URL_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    value = value.split("/api/")[0].rstrip("/")
    # Пользователь мог записать в config.py уже OpenAI-совместимый адрес
    # или просто корень /api без завершающего слеша.
    if value.endswith("/api"):
        value = value[: -len("/api")]
    if value.endswith("/v1"):
        value = value[: -len("/v1")]
    return value


def ollama_native_base(settings: dict | None = None) -> str:
    """Корень Ollama (``http://host:port``) — для нативных запросов вроде /api/tags."""
    override = (settings or {}).get("ollama_base_url")
    if override:
        host = _normalize_host(override)
    else:
        host = _normalize_host(_config_value("OLLAMA_URL", ""))
    return host or DEFAULT_OLLAMA_BASE_URL.rsplit("/v1", 1)[0]


def ollama_base_url(settings: dict | None = None) -> str:
    """OpenAI-совместимый адрес Ollama: ``http://host:port/v1``."""
    override = (settings or {}).get("ollama_base_url")
    if override:
        normalized = str(override).strip().rstrip("/")
        if normalized.endswith("/v1"):
            return normalized
    host = ollama_native_base(settings)
    return f"{host.rstrip('/')}/v1"


# --------------------------------------------------------------------------- #
#  Конфигурация и фабрика
# --------------------------------------------------------------------------- #
@dataclass
class ProviderConfig:
    """Всё, что нужно, чтобы обратиться к провайдеру."""

    name: str          # "ollama" | "deepseek" | "openai" | ...
    base_url: str      # префикс API, запрос уйдёт на {base_url}/chat/completions
    api_key: str | None
    model: str
    #: Требуется ли реальный ключ. У Ollama он фиктивный (заголовок нужен,
    #: но сервер его не проверяет), поэтому по умолчанию False.
    requires_api_key: bool = False

    @property
    def label(self) -> str:
        """Имя провайдера для сообщений пользователю: «Ollama», «DeepSeek»."""
        return _DISPLAY_NAMES.get(self.name, str(self.name).title())


def _ollama_config(settings: dict) -> ProviderConfig:
    model = str(settings.get("ollama_model") or "").strip()
    return ProviderConfig(
        name="ollama",
        base_url=ollama_base_url(settings),
        # Ollama не проверяет ключ, но заголовок Authorization должен быть:
        # так запрос выглядит как обычный OpenAI-клиент для любого прокси.
        api_key=str(settings.get("ollama_api_key") or "ollama"),
        model=model or str(_config_value("MODEL_NAME", DEFAULT_OLLAMA_MODEL)),
    )


def _deepseek_config(settings: dict) -> ProviderConfig:
    model = str(settings.get("deepseek_model") or "").strip()
    return ProviderConfig(
        name="deepseek",
        base_url=DEEPSEEK_BASE_URL,
        api_key=str(settings.get("api_key") or "").strip() or None,
        model=model or DEFAULT_DEEPSEEK_MODEL,
        requires_api_key=True,
    )


#: Добавление нового провайдера = одна строка. Достаточно вернуть ProviderConfig
#: с OpenAI-совместимым ``base_url``: обработка ошибок, повторов и логирование
#: уже общие.
PROVIDERS = {
    "ollama": _ollama_config,
    "deepseek": _deepseek_config,
}


def supported_providers() -> tuple[str, ...]:
    """Имена известных провайдеров (для валидации настроек и тестов)."""
    return tuple(PROVIDERS)


def make_provider(
    provider_name: str,
    settings: dict | None = None,
    model: str | None = None,
    timeout: int = 120,
    retry_wait_limit: float | None = None,
) -> "OpenAICompatibleProvider":
    """Создаёт провайдера по имени из настроек — единственная точка создания.

    ``model`` позволяет переопределить модель из настроек (нужно лору, который
    получает модель отдельным словарём). ``retry_wait_limit`` — явный порог
    ожидания для 429; по умолчанию берётся из config.MAX_RETRY_WAIT_SECONDS.
    """
    name = str(provider_name or "ollama").strip().lower()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise ValueError(f"Неизвестный провайдер: {provider_name}")

    config = factory(dict(settings or {}))
    if model and str(model).strip():
        config.model = str(model).strip()
    if config.requires_api_key and not config.api_key:
        raise PermissionError(f"Не указан API-ключ для провайдера «{config.label}»")
    return OpenAICompatibleProvider(
        config, timeout=timeout, retry_wait_limit=retry_wait_limit
    )


def settings_fingerprint(settings: dict | None) -> tuple:
    """Короткий отпечаток настроек, от которых зависит провайдер.

    main.py создаёт провайдера один раз, но пересоздаёт его, когда пользователь
    сменил провайдера/модель/ключ в лаунчере — так сохраняется прежнее
    поведение «смена настроек подхватывается без перезапуска переводчика».
    """
    data = settings or {}
    return (
        str(data.get("provider") or "").strip().lower(),
        str(data.get("ollama_model") or "").strip(),
        str(data.get("deepseek_model") or "").strip(),
        str(data.get("api_key") or "").strip(),
        str(data.get("ollama_base_url") or "").strip(),
    )


# --------------------------------------------------------------------------- #
#  Разбор служебных заголовков и тел ответов
# --------------------------------------------------------------------------- #
def parse_retry_after(value) -> float | None:
    """Разбирает ``Retry-After``: секунды или HTTP-дата.

    Возвращает ``None``, если заголовка нет или он неразборчив. В этом случае
    задержку НЕ выдумываем и запрос не повторяем: лучше сразу показать
    сообщение, чем заморозить перевод на «придуманные» секунды.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass

    try:
        moment = parsedate_to_datetime(text)
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _error_message(response) -> str:
    """Достаёт человеческий текст ошибки из тела ответа, если он там есть."""
    try:
        data = response.json()
    except Exception:
        return (getattr(response, "text", "") or "")[:200]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail"):
            if data.get(key):
                return str(data[key])
    return (getattr(response, "text", "") or "")[:200]


def _extract_content(data) -> str:
    """Достаёт ``choices[0].message.content`` из OpenAI-совместимого ответа.

    Сообщения об ошибке намеренно без префикса ``[provider]``: их показывает
    интерфейс («[Ошибка Ollama: ...]»), дублировать имя провайдера не нужно.
    """
    if not isinstance(data, dict):
        raise ProviderError("Некорректный ответ API: ожидался JSON-объект")
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise ProviderError("В ответе API нет поля choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError("Некорректный элемент choices в ответе API")
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ProviderError("Некорректное поле message в ответе API")
    content = message.get("content")
    if isinstance(content, list):
        # Некоторые OpenAI-совместимые серверы отдают список частей.
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "").strip()


# --------------------------------------------------------------------------- #
#  Провайдер
# --------------------------------------------------------------------------- #
class OpenAICompatibleProvider:
    """Клиент любого OpenAI-совместимого API (Ollama, DeepSeek, OpenAI, ...)."""

    def __init__(
        self,
        config: ProviderConfig,
        timeout: int = 120,
        retry_wait_limit: float | None = None,
    ):
        self.config = config
        self.timeout = timeout
        #: Явный порог ожидания для 429; None — брать из config.py.
        self.retry_wait_limit = retry_wait_limit
        self.session = requests.Session()
        self.session.headers.update(self._build_headers())

    # --- служебное -------------------------------------------------------- #
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def label(self) -> str:
        return self.config.label

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _retry_limit(self) -> float:
        if self.retry_wait_limit is not None:
            return float(self.retry_wait_limit)
        return max_retry_wait_seconds()

    def _build_payload(
        self,
        prompt: str,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
        top_p: float | None,
        presence_penalty: float | None,
    ) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        # Передаём только то, что реально задано: лишние ключи часть серверов
        # отвергает, а часть молча игнорирует (проверено на Ollama /v1).
        if top_p is not None:
            payload["top_p"] = top_p
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        return payload

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "OpenAICompatibleProvider":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- основной вызов --------------------------------------------------- #
    def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.7,
        top_p: float | None = None,
        presence_penalty: float | None = None,
        timeout: float | None = None,
    ) -> str:
        """Отправляет один запрос ``/chat/completions`` и возвращает текст ответа.

        Ошибки не превращаются в строки: вызывающий код получает исключение и сам
        решает, что показать пользователю (перевод) или что просто пропустить
        молча с предупреждением в журнале (лор).
        """
        payload = self._build_payload(
            prompt, system_prompt, max_tokens, temperature, top_p, presence_penalty
        )
        url = self._url()
        request_timeout = self.timeout if timeout is None else timeout
        prompt_chars = len(prompt or "") + len(system_prompt or "")

        retried = False
        while True:
            started = time.perf_counter()
            try:
                response = self.session.post(
                    url, json=payload, timeout=request_timeout
                )
            except requests.Timeout:
                log.error(
                    "[%s] Timeout after %ss — модель не ответила вовремя",
                    self.name,
                    request_timeout,
                )
                raise
            except requests.RequestException as exc:
                log.error("[%s] Network error: %s", self.name, exc)
                raise

            elapsed = time.perf_counter() - started

            if response.status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    # Сервер не сказал, сколько ждать: не угадываем (иначе при
                    # большом MAX_RETRY_WAIT_SECONDS можно заморозить перевод).
                    log.warning(
                        "[%s] 429 Too Many Requests, Retry-After не указан — "
                        "повторять не буду",
                        self.name,
                    )
                    raise RateLimitError(self.name, None)

                limit = self._retry_limit()
                if not retried and retry_after <= limit:
                    retried = True
                    log.warning(
                        "[%s] 429 Too Many Requests, retry after %gs — "
                        "повторяю запрос один раз (порог %gs)",
                        self.name,
                        retry_after,
                        limit,
                    )
                    time.sleep(retry_after)
                    continue
                if retried:
                    log.warning(
                        "[%s] 429 Too Many Requests, retry after %gs — "
                        "повтор уже был, сдаюсь",
                        self.name,
                        retry_after,
                    )
                else:
                    log.warning(
                        "[%s] 429 Too Many Requests, retry after %gs — "
                        "повторять не буду (порог %gs)",
                        self.name,
                        retry_after,
                        limit,
                    )
                raise RateLimitError(self.name, retry_after)

            if response.status_code in (401, 403):
                message = _error_message(response)
                if response.status_code == 401:
                    log.error(
                        "[%s] HTTP 401 Unauthorized — неверный API-ключ (%s)",
                        self.name,
                        message,
                    )
                    raise PermissionError(f"Неверный API-ключ для {self.label}")
                log.error(
                    "[%s] HTTP 403 Forbidden — доступ запрещён (%s)",
                    self.name,
                    message,
                )
                raise PermissionError(f"Доступ запрещён для {self.label} (HTTP 403)")

            if not response.ok:
                message = _error_message(response)
                log.error(
                    "[%s] HTTP %s: %s",
                    self.name,
                    response.status_code,
                    str(message)[:200],
                )
                raise ProviderHTTPError(self.name, response.status_code, str(message))

            try:
                data = response.json()
            except ValueError as exc:
                log.error("[%s] Некорректный JSON в ответе: %s", self.name, exc)
                raise ProviderError("Некорректный JSON в ответе API") from exc

            content = _extract_content(data)
            log.info(
                "[%s] %d → %d chars in %.2fs",
                self.name,
                prompt_chars,
                len(content),
                elapsed,
            )
            return content
