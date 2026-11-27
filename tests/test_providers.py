# tests/test_providers.py
"""Тесты единой абстракции LLM-провайдеров (блок 1 релиза 1.6.0).

Здесь проверяется транспорт: адреса, заголовки, тело запроса, разбор ответа,
ошибки (429 / 401 / 5xx / таймаут / сеть), ограниченный повтор для 429 и
фабрика провайдеров. Лор-логика поверх провайдера проверяется в test_lore.py.

Все тесты работают без сети: ``requests.Session.post`` подменяется моком.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import brain.llm_engine as llm  # noqa: E402
from brain import providers  # noqa: E402
from brain.providers import (  # noqa: E402
    DEEPSEEK_BASE_URL,
    ProviderError,
    ProviderHTTPError,
    RateLimitError,
    make_provider,
    max_retry_wait_seconds,
    ollama_base_url,
    ollama_native_base,
    parse_retry_after,
    settings_fingerprint,
    supported_providers,
)


class FakeResponse:
    """Минимальная замена requests.Response для моков."""

    def __init__(self, payload=None, status_code=200, headers=None, text=None):
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = (
            text
            if text is not None
            else json.dumps(self._payload, ensure_ascii=False)
        )

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class BrokenJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("Expecting value")


def ollama_provider(**kwargs):
    """Провайдер Ollama с детерминированным адресом (не зависит от config.py)."""
    settings = {
        "ollama_model": "qwen2.5:3b",
        "ollama_base_url": "http://localhost:11434",
    }
    return make_provider("ollama", settings, **kwargs)


def deepseek_provider(**kwargs):
    settings = {"api_key": "sk-test", "deepseek_model": "deepseek-chat"}
    return make_provider("deepseek", settings, **kwargs)


def patch_post(**kwargs):
    return mock.patch.object(providers.requests.Session, "post", **kwargs)


# --------------------------------------------------------------------------- #
#  Конфигурация провайдеров и фабрика
# --------------------------------------------------------------------------- #
class ProviderConfigTests(unittest.TestCase):
    def test_ollama_defaults(self):
        provider = ollama_provider()
        self.addCleanup(provider.close)
        self.assertEqual(provider.config.name, "ollama")
        self.assertEqual(provider.config.model, "qwen2.5:3b")
        self.assertEqual(provider.config.base_url, "http://localhost:11434/v1")
        self.assertFalse(provider.config.requires_api_key)
        # Ollama не проверяет ключ, но заголовок нужен для совместимости.
        self.assertEqual(provider.session.headers["Authorization"], "Bearer ollama")

    def test_deepseek_uses_cloud_and_key(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        self.assertEqual(provider.config.name, "deepseek")
        self.assertEqual(provider.config.base_url, DEEPSEEK_BASE_URL)
        self.assertEqual(provider.config.model, "deepseek-chat")
        self.assertEqual(provider.session.headers["Authorization"], "Bearer sk-test")
        self.assertTrue(provider.config.requires_api_key)

    def test_deepseek_without_key_raises(self):
        with self.assertRaises(PermissionError) as caught:
            make_provider("deepseek", {"api_key": "", "deepseek_model": "deepseek-chat"})
        self.assertIn("DeepSeek", str(caught.exception))

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            make_provider("gemini", {})

    def test_model_override(self):
        provider = ollama_provider(model="translategemma:4b")
        self.addCleanup(provider.close)
        self.assertEqual(provider.config.model, "translategemma:4b")

    def test_empty_provider_name_defaults_to_ollama(self):
        provider = make_provider("", {"ollama_model": "qwen2.5:3b",
                                      "ollama_base_url": "http://localhost:11434"})
        self.addCleanup(provider.close)
        self.assertEqual(provider.config.name, "ollama")

    def test_supported_providers(self):
        self.assertIn("ollama", supported_providers())
        self.assertIn("deepseek", supported_providers())

    def test_label_is_human_readable(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        self.assertEqual(provider.label, "DeepSeek")


class ProviderUrlTests(unittest.TestCase):
    def test_base_url_comes_from_configured_ollama_url(self):
        fake_config = SimpleNamespace(OLLAMA_URL="http://192.168.0.5:11434/api/generate")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(ollama_base_url(), "http://192.168.0.5:11434/v1")
            self.assertEqual(ollama_native_base(), "http://192.168.0.5:11434")

    def test_base_url_without_suffix(self):
        fake_config = SimpleNamespace(OLLAMA_URL="http://192.168.0.5:11434")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(ollama_base_url(), "http://192.168.0.5:11434/v1")

    def test_base_url_is_not_doubled_for_v1(self):
        fake_config = SimpleNamespace(OLLAMA_URL="http://host:11434/v1")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(ollama_base_url(), "http://host:11434/v1")

    def test_settings_override_wins(self):
        self.assertEqual(
            ollama_base_url({"ollama_base_url": "http://other:1234"}),
            "http://other:1234/v1",
        )

    def test_settings_override_with_v1_is_not_doubled(self):
        self.assertEqual(
            ollama_base_url({"ollama_base_url": "http://other:1234/v1"}),
            "http://other:1234/v1",
        )

    def test_api_root_without_slash(self):
        fake_config = SimpleNamespace(OLLAMA_URL="http://host:11434/api")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(ollama_base_url(), "http://host:11434/v1")

    def test_api_chat_suffix(self):
        fake_config = SimpleNamespace(OLLAMA_URL="http://host:11434/api/chat")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(ollama_base_url(), "http://host:11434/v1")

    def test_missing_config_falls_back_to_localhost(self):
        with mock.patch.object(providers, "_APP_CONFIG", False):
            self.assertEqual(ollama_base_url(), providers.DEFAULT_OLLAMA_BASE_URL)
            self.assertEqual(
                ollama_native_base(), "http://localhost:11434"
            )


class RetryAfterParsingTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_retry_after("30"), 30.0)
        self.assertEqual(parse_retry_after("0"), 0.0)

    def test_missing_is_none(self):
        # Нет заголовка -> задержку не выдумываем (иначе можно заморозить перевод).
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))

    def test_garbage_is_none(self):
        self.assertIsNone(parse_retry_after("soon"))

    def test_http_date(self):
        # Дата в будущем -> положительное число секунд.
        self.assertGreater(parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 0)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(parse_retry_after("-5"), 0.0)


class RetryWaitConfigTests(unittest.TestCase):
    def test_reads_threshold_from_config(self):
        fake_config = SimpleNamespace(MAX_RETRY_WAIT_SECONDS=7)
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(max_retry_wait_seconds(), 7.0)

    def test_broken_value_falls_back(self):
        fake_config = SimpleNamespace(MAX_RETRY_WAIT_SECONDS="много")
        with mock.patch.object(providers, "_APP_CONFIG", fake_config):
            self.assertEqual(
                max_retry_wait_seconds(), providers.DEFAULT_RETRY_WAIT_SECONDS
            )

    def test_missing_value_falls_back(self):
        with mock.patch.object(providers, "_APP_CONFIG", SimpleNamespace()):
            self.assertEqual(
                max_retry_wait_seconds(), providers.DEFAULT_RETRY_WAIT_SECONDS
            )


class SettingsFingerprintTests(unittest.TestCase):
    def test_same_settings_same_fingerprint(self):
        first = {"provider": "ollama", "ollama_model": "qwen2.5:3b", "api_key": ""}
        second = dict(first)
        self.assertEqual(settings_fingerprint(first), settings_fingerprint(second))

    def test_model_change_changes_fingerprint(self):
        first = {"provider": "ollama", "ollama_model": "qwen2.5:3b"}
        second = {"provider": "ollama", "ollama_model": "translategemma:4b"}
        self.assertNotEqual(settings_fingerprint(first), settings_fingerprint(second))

    def test_key_change_changes_fingerprint(self):
        first = {"provider": "deepseek", "api_key": "sk-one"}
        second = {"provider": "deepseek", "api_key": "sk-two"}
        self.assertNotEqual(settings_fingerprint(first), settings_fingerprint(second))

    def test_provider_change_changes_fingerprint(self):
        first = {"provider": "ollama"}
        second = {"provider": "deepseek"}
        self.assertNotEqual(settings_fingerprint(first), settings_fingerprint(second))


# --------------------------------------------------------------------------- #
#  Успешный запрос
# --------------------------------------------------------------------------- #
class GenerateSuccessTests(unittest.TestCase):
    def setUp(self):
        self.provider = ollama_provider()
        self.addCleanup(self.provider.close)

    def test_request_shape_and_result(self):
        fake = FakeResponse({"choices": [{"message": {"content": "  ответ  "}}]})
        with patch_post(return_value=fake) as post:
            result = self.provider.generate(
                "текст", system_prompt="система", max_tokens=128, temperature=0.0
            )

        self.assertEqual(result, "ответ")
        url = post.call_args.args[0]
        self.assertEqual(url, "http://localhost:11434/v1/chat/completions")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen2.5:3b")
        self.assertEqual(payload["max_tokens"], 128)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertFalse(payload["stream"])
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "система"},
                {"role": "user", "content": "текст"},
            ],
        )

    def test_system_prompt_optional(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            self.provider.generate("только текст")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"], [{"role": "user", "content": "только текст"}])

    def test_optional_params_are_omitted_when_not_set(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            self.provider.generate("x")
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("top_p", payload)
        self.assertNotIn("presence_penalty", payload)

    def test_optional_params_are_forwarded(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            self.provider.generate("x", top_p=0.4, presence_penalty=0.0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["top_p"], 0.4)
        self.assertEqual(payload["presence_penalty"], 0.0)

    def test_timeout_is_passed_to_request(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            self.provider.generate("x", timeout=7)
        self.assertEqual(post.call_args.kwargs["timeout"], 7)

    def test_logs_lengths_and_elapsed_time(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ответ"}}]})
        with patch_post(return_value=fake), self.assertLogs(
            providers.log, level="INFO"
        ) as captured:
            self.provider.generate("запрос")
        text = "\n".join(captured.output)
        self.assertIn("chars in", text)
        self.assertIn("ollama", text)

    def test_list_content_is_joined(self):
        fake = FakeResponse(
            {"choices": [{"message": {"content": [
                {"type": "text", "text": "часть1"},
                {"type": "text", "text": "часть2"},
            ]}}]}
        )
        with patch_post(return_value=fake):
            self.assertEqual(self.provider.generate("x"), "часть1часть2")

    def test_empty_content_returns_empty_string(self):
        fake = FakeResponse({"choices": [{"message": {"content": ""}}]})
        with patch_post(return_value=fake):
            self.assertEqual(self.provider.generate("x"), "")


# --------------------------------------------------------------------------- #
#  HTTP 429 — ограниченный повтор
# --------------------------------------------------------------------------- #
class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.provider = ollama_provider(retry_wait_limit=5)
        self.addCleanup(self.provider.close)

    def test_retries_once_when_retry_after_within_limit(self):
        responses = [
            FakeResponse({"error": {"message": "slow down"}}, status_code=429,
                         headers={"Retry-After": "2"}),
            FakeResponse({"choices": [{"message": {"content": "готово"}}]}),
        ]
        with patch_post(side_effect=responses) as post, mock.patch.object(
            providers.time, "sleep"
        ) as sleep:
            result = self.provider.generate("x")

        self.assertEqual(result, "готово")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_no_retry_when_retry_after_above_limit(self):
        fake = FakeResponse({"error": {"message": "slow down"}}, status_code=429,
                            headers={"Retry-After": "30"})
        with patch_post(return_value=fake) as post, mock.patch.object(
            providers.time, "sleep"
        ) as sleep:
            with self.assertRaises(RateLimitError) as caught:
                self.provider.generate("x")

        self.assertEqual(caught.exception.retry_after, 30.0)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_retries_only_once(self):
        fake = FakeResponse({"error": {"message": "slow down"}}, status_code=429,
                            headers={"Retry-After": "1"})
        with patch_post(return_value=fake) as post, mock.patch.object(
            providers.time, "sleep"
        ):
            with self.assertRaises(RateLimitError):
                self.provider.generate("x")
        self.assertEqual(post.call_count, 2)

    def test_missing_retry_after_means_no_retry(self):
        fake = FakeResponse({"error": {"message": "slow down"}}, status_code=429)
        with patch_post(return_value=fake) as post, mock.patch.object(
            providers.time, "sleep"
        ) as sleep:
            with self.assertRaises(RateLimitError) as caught:
                self.provider.generate("x")
        # Без заголовка задержку не выдумываем: retry_after = None, один запрос.
        self.assertIsNone(caught.exception.retry_after)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_missing_retry_after_does_not_retry_even_with_high_threshold(self):
        provider = ollama_provider(retry_wait_limit=120)
        self.addCleanup(provider.close)
        fake = FakeResponse({"error": {"message": "slow down"}}, status_code=429)
        with patch_post(return_value=fake) as post, mock.patch.object(
            providers.time, "sleep"
        ) as sleep:
            with self.assertRaises(RateLimitError):
                provider.generate("x")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_zero_threshold_disables_retry(self):
        provider = ollama_provider(retry_wait_limit=0)
        self.addCleanup(provider.close)
        fake = FakeResponse({"error": {"message": "slow down"}}, status_code=429,
                            headers={"Retry-After": "1"})
        with patch_post(return_value=fake) as post:
            with self.assertRaises(RateLimitError):
                provider.generate("x")
        self.assertEqual(post.call_count, 1)

    def test_threshold_comes_from_config_when_not_forced(self):
        provider = ollama_provider()
        self.addCleanup(provider.close)
        fake_config = SimpleNamespace(MAX_RETRY_WAIT_SECONDS=10)
        responses = [
            FakeResponse({"error": {"message": "slow down"}}, status_code=429,
                         headers={"Retry-After": "8"}),
            FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
        ]
        with mock.patch.object(providers, "_APP_CONFIG", fake_config), \
                patch_post(side_effect=responses) as post, \
                mock.patch.object(providers.time, "sleep"):
            self.assertEqual(provider.generate("x"), "ok")
        self.assertEqual(post.call_count, 2)


# --------------------------------------------------------------------------- #
#  Остальные ошибки
# --------------------------------------------------------------------------- #
class ProviderErrorTests(unittest.TestCase):
    def setUp(self):
        self.provider = deepseek_provider()
        self.addCleanup(self.provider.close)

    def test_401_raises_permission_error(self):
        fake = FakeResponse({"error": {"message": "Authentication Fails"}},
                            status_code=401)
        with patch_post(return_value=fake) as post, self.assertLogs(
            providers.log, level="ERROR"
        ) as captured:
            with self.assertRaises(PermissionError) as caught:
                self.provider.generate("x")
        self.assertIn("DeepSeek", str(caught.exception))
        self.assertIn("Unauthorized", "\n".join(captured.output))
        # Политика: повторяем ТОЛЬКО 429, остальные ошибки — одна попытка.
        self.assertEqual(post.call_count, 1)

    def test_403_is_not_reported_as_bad_key(self):
        fake = FakeResponse({"error": {"message": "forbidden"}}, status_code=403)
        with patch_post(return_value=fake) as post:
            with self.assertRaises(PermissionError) as caught:
                self.provider.generate("x")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("Неверный API-ключ", str(caught.exception))
        self.assertEqual(post.call_count, 1)

    def test_5xx_raises_provider_http_error_with_api_message(self):
        fake = FakeResponse({"error": {"message": "server exploded"}}, status_code=500)
        with patch_post(return_value=fake) as post, self.assertLogs(
            providers.log, level="ERROR"
        ):
            with self.assertRaises(ProviderHTTPError) as caught:
                self.provider.generate("x")
        self.assertEqual(caught.exception.status_code, 500)
        self.assertIn("server exploded", str(caught.exception))
        self.assertEqual(post.call_count, 1)

    def test_timeout_propagates_and_is_logged(self):
        with patch_post(side_effect=requests.Timeout("timed out")) as post, \
                self.assertLogs(providers.log, level="ERROR") as captured:
            with self.assertRaises(requests.Timeout):
                self.provider.generate("x")
        self.assertIn("Timeout", "\n".join(captured.output))
        self.assertEqual(post.call_count, 1)

    def test_network_error_propagates(self):
        with patch_post(side_effect=requests.ConnectionError("no route")) as post, \
                self.assertLogs(providers.log, level="ERROR"):
            with self.assertRaises(requests.ConnectionError):
                self.provider.generate("x")
        self.assertEqual(post.call_count, 1)

    def test_missing_choices_raises_provider_error(self):
        fake = FakeResponse({"unexpected": "shape"})
        with patch_post(return_value=fake):
            with self.assertRaises(ProviderError):
                self.provider.generate("x")

    def test_non_dict_choice_raises_provider_error(self):
        # Не должно вылетать AttributeError: контракт — всегда ProviderError.
        fake = FakeResponse({"choices": ["not-a-dict"]})
        with patch_post(return_value=fake):
            with self.assertRaises(ProviderError):
                self.provider.generate("x")

    def test_non_dict_message_raises_provider_error(self):
        fake = FakeResponse({"choices": [{"message": "oops"}]})
        with patch_post(return_value=fake):
            with self.assertRaises(ProviderError):
                self.provider.generate("x")

    def test_malformed_json_raises_provider_error(self):
        with patch_post(return_value=BrokenJsonResponse()):
            with self.assertRaises(ProviderError):
                self.provider.generate("x")

    def test_session_is_reused_between_calls(self):
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        session = self.provider.session
        with patch_post(return_value=fake) as post:
            self.provider.generate("one")
            self.provider.generate("two")
        self.assertEqual(post.call_count, 2)
        # Оба вызова идут через один и тот же requests.Session (keep-alive).
        self.assertIs(self.provider.session, session)

    def test_close_closes_session(self):
        session = self.provider.session
        with mock.patch.object(session, "close") as close:
            self.provider.close()
        close.assert_called_once()


# --------------------------------------------------------------------------- #
#  translate() поверх провайдера
# --------------------------------------------------------------------------- #
class TranslateWithProviderTests(unittest.TestCase):
    """translate() — единственная точка входа перевода; провайдер передаётся снаружи."""

    def setUp(self):
        patcher = mock.patch.object(
            llm, "_build_translation_prompt", return_value=("система", "пользователь")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        saver = mock.patch.object(llm, "save_memory")
        self.save_memory = saver.start()
        self.addCleanup(saver.stop)

    def test_returns_text_and_saves_memory(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": "  Привет  "}}]})
        with patch_post(return_value=fake):
            result = llm.translate("Hello", provider=provider)

        self.assertEqual(result, "Привет")
        self.save_memory.assert_called_once_with("Hello", "Привет")

    def test_passes_config_generation_options(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            llm.translate("Hello", provider=provider)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["max_tokens"], llm.config.OPTIONS["num_predict"])
        self.assertEqual(payload["temperature"], llm.config.OPTIONS["temperature"])
        self.assertEqual(payload["top_p"], llm.config.OPTIONS["top_p"])

    def test_429_returns_existing_message(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"error": {"message": "Rate limit reached"}},
                            status_code=429, headers={"Retry-After": "30"})
        with patch_post(return_value=fake):
            result = llm.translate("Hello", provider=provider)

        self.assertEqual(
            result,
            "[Ошибка DeepSeek: превышен лимит запросов (429). "
            "Подожди немного и повтори.]",
        )
        self.save_memory.assert_not_called()

    def test_401_returns_error_message(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"error": {"message": "bad key"}}, status_code=401)
        with patch_post(return_value=fake):
            result = llm.translate("Hello", provider=provider)

        self.assertIn("DeepSeek", result)
        self.assertIn("API-ключ", result)
        self.save_memory.assert_not_called()

    def test_empty_translation_message(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": ""}}]})
        with patch_post(return_value=fake):
            result = llm.translate("Hello", provider=provider)

        self.assertEqual(result, "[Модель не смогла перевести]")
        self.save_memory.assert_not_called()

    def test_none_translation_message(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": "None"}}]})
        with patch_post(return_value=fake):
            result = llm.translate("Hello", provider=provider)

        self.assertEqual(result, "[Модель не смогла перевести]")

    def test_timeout_returns_friendly_message(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        with patch_post(side_effect=requests.Timeout("late")):
            result = llm.translate("Hello", provider=provider)

        self.assertIn("не отвечает", result)
        self.assertIn("DeepSeek", result)

    def test_without_provider_builds_from_settings(self):
        settings = {"provider": "deepseek", "api_key": "sk-test",
                    "deepseek_model": "deepseek-chat"}
        fake_provider = mock.Mock()
        fake_provider.label = "DeepSeek"
        fake_provider.generate.return_value = "Ок"
        with mock.patch.object(llm, "make_provider", return_value=fake_provider) as factory:
            result = llm.translate("Hello", settings=settings)

        factory.assert_called_once_with("deepseek", settings)
        self.assertEqual(result, "Ок")

    def test_bare_translate_reads_settings_itself(self):
        # Прежний вызов translate(text) должен работать без аргументов и брать
        # модель/ключ из настроек, а не значения по умолчанию.
        settings = {"provider": "ollama", "ollama_model": "qwen2.5:3b", "api_key": ""}
        fake_provider = mock.Mock()
        fake_provider.label = "Ollama"
        fake_provider.generate.return_value = "Ок"
        with mock.patch.object(llm, "load_settings", return_value=settings), \
                mock.patch.object(
                    llm, "make_provider", return_value=fake_provider
                ) as factory:
            result = llm.translate("Hello")

        factory.assert_called_once_with("ollama", settings)
        self.assertEqual(result, "Ок")
        # Провайдер создан внутри translate() — он же его и закрывает.
        fake_provider.close.assert_called_once()

    def test_passed_provider_is_not_closed(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake), mock.patch.object(provider, "close") as close:
            llm.translate("Hello", provider=provider)
        close.assert_not_called()

    def test_unknown_provider_setting_falls_back_to_ollama(self):
        fake_provider = mock.Mock()
        fake_provider.label = "Ollama"
        fake_provider.generate.return_value = "Ок"
        with mock.patch.object(llm, "make_provider", return_value=fake_provider) as factory:
            llm.translate("Hello", settings={"provider": "gemini"})

        self.assertEqual(factory.call_args.args[0], "ollama")

    def test_provider_creation_error_returns_message(self):
        with mock.patch.object(
            llm, "make_provider", side_effect=PermissionError("нет ключа")
        ):
            result = llm.translate("Hello", settings={"provider": "deepseek"})
        self.assertIn("нет ключа", result)

    def test_unreadable_settings_does_not_raise(self):
        # Если settings.json не читается, перевод не должен падать исключением.
        fake_provider = mock.Mock()
        fake_provider.label = "Ollama"
        fake_provider.generate.return_value = "Ок"
        with mock.patch.object(llm, "load_settings", side_effect=OSError("boom")), \
                mock.patch.object(
                    llm, "make_provider", return_value=fake_provider
                ) as factory:
            result = llm.translate("Hello")

        factory.assert_called_once_with("ollama", None)
        self.assertEqual(result, "Ок")
        fake_provider.close.assert_called_once()

    def test_system_prompt_override_is_used(self):
        provider = deepseek_provider()
        self.addCleanup(provider.close)
        fake = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch_post(return_value=fake) as post:
            llm.translate("Hello", provider=provider, system_prompt="СВОЙ ПРОМПТ")
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "СВОЙ ПРОМПТ"})


class UnknownTermsTests(unittest.TestCase):
    """Проверка неизвестных модели слов тоже идёт через провайдера."""

    def test_uses_provider_and_parses_json_array(self):
        provider = mock.MagicMock()
        provider.__enter__.return_value = provider  # with ... as provider:
        provider.generate.return_value = '["Jia Xichun", "Ryoshu"]'
        with mock.patch.object(llm, "make_provider", return_value=provider) as factory:
            terms = llm._unknown_terms_from_model("some text")

        self.assertEqual(terms, ["Jia Xichun", "Ryoshu"])
        self.assertEqual(factory.call_args.args[0], "ollama")
        # Модель берётся из config.MODEL_NAME, как до рефакторинга: настройки
        # на каждом кадре не читаем.
        self.assertEqual(
            factory.call_args.args[1], {"ollama_model": llm.config.MODEL_NAME}
        )
        provider.__enter__.assert_called_once()
        provider.__exit__.assert_called_once()

    def test_provider_failure_returns_empty(self):
        with mock.patch.object(llm, "make_provider", side_effect=OSError("down")):
            self.assertEqual(llm._unknown_terms_from_model("text"), [])

    def test_garbage_response_returns_empty(self):
        provider = mock.MagicMock()
        provider.__enter__.return_value = provider
        provider.generate.return_value = "no json here"
        with mock.patch.object(llm, "make_provider", return_value=provider):
            self.assertEqual(llm._unknown_terms_from_model("text"), [])


class ProviderHotSwapTests(unittest.TestCase):
    """Провайдер создаётся в main.py один раз, но переживает смену настроек."""

    def setUp(self):
        import main as main_module

        self.main = main_module
        self.settings = {
            "provider": "ollama",
            "ollama_model": "translategemma:4b",
            "deepseek_model": "deepseek-chat",
            "api_key": "",
        }

    def test_provider_created_once_when_settings_do_not_change(self):
        provider = mock.Mock()
        translate = mock.Mock(return_value="ok")
        with mock.patch.object(
            self.main, "make_provider", return_value=provider
        ) as factory, mock.patch.object(
            self.main, "load_settings", return_value=dict(self.settings)
        ):
            wrapped = self.main._translator_with_provider(translate, self.settings)
            wrapped("one")
            wrapped("two")

        factory.assert_called_once()
        self.assertEqual(translate.call_count, 2)
        self.assertIs(translate.call_args.kwargs["provider"], provider)

    def test_provider_rebuilt_when_model_changes(self):
        first, second = mock.Mock(), mock.Mock()
        translate = mock.Mock(return_value="ok")
        changed = dict(self.settings, ollama_model="qwen2.5:3b")
        with mock.patch.object(
            self.main, "make_provider", side_effect=[first, second]
        ) as factory, mock.patch.object(
            self.main, "load_settings", side_effect=[dict(self.settings), changed]
        ):
            wrapped = self.main._translator_with_provider(translate, self.settings)
            wrapped("one")
            wrapped("two")

        self.assertEqual(factory.call_count, 2)
        self.assertIs(translate.call_args.kwargs["provider"], second)
        # Старый провайдер закрывается при пересоздании (иначе копятся сессии).
        first.close.assert_called_once()

    def test_failed_creation_falls_back_to_plain_translate(self):
        translate = mock.Mock(return_value="[Ошибка: нет ключа]")
        with mock.patch.object(
            self.main, "make_provider", side_effect=PermissionError("нет ключа")
        ), mock.patch.object(
            self.main, "load_settings", return_value=dict(self.settings)
        ):
            wrapped = self.main._translator_with_provider(translate, self.settings)
            result = wrapped("x")

        # Никакого исключения в цикл захвата: translate сам вернёт сообщение.
        translate.assert_called_once_with("x")
        self.assertEqual(result, "[Ошибка: нет ключа]")

    def test_unknown_provider_name_falls_back_to_ollama(self):
        with mock.patch.object(self.main, "make_provider") as factory:
            self.main._create_llm_provider({"provider": "gemini"})
        self.assertEqual(factory.call_args.args[0], "ollama")


if __name__ == "__main__":
    unittest.main()
