# tests/test_lore.py
"""Тесты анализа лора персонажей (v1.5.2).

Главное, что проверяем: лор считает модель, выбранная для перевода, а не
жёстко зашитая gemma2:9b; при недоступном провайдере лор не сохраняется, а
приложение продолжает работать.

Все тесты работают без сети и не пишут в data/: сеть и сохранение замоканы.
"""

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.llm_engine as llm  # noqa: E402
from brain.providers import (  # noqa: E402
    ProviderHTTPError,
    RateLimitError,
    make_provider,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _fake_provider(label, content=None, side_effect=None):
    """Заглушка провайдера: _lore_completion должен работать только через неё."""
    provider = mock.Mock()
    provider.label = label
    if side_effect is not None:
        provider.generate.side_effect = side_effect
    else:
        provider.generate.return_value = content
    return provider


def _clear_lore_state():
    with llm._LORE_LOCK:
        llm._LORE_INFLIGHT.clear()
        llm._LORE_LAST_ATTEMPT.clear()


class LoreModelConfigTests(unittest.TestCase):
    def test_ollama_uses_translation_model(self):
        cfg = llm.lore_model_config(
            {"provider": "ollama", "ollama_model": "qwen2.5:3b", "api_key": ""}
        )
        self.assertEqual(cfg["provider"], "ollama")
        self.assertEqual(cfg["model"], "qwen2.5:3b")

    def test_deepseek_uses_cloud_model_and_key(self):
        cfg = llm.lore_model_config(
            {
                "provider": "DeepSeek",
                "ollama_model": "translategemma:4b",
                "deepseek_model": "deepseek-chat",
                "api_key": "sk-test",
            }
        )
        self.assertEqual(cfg["provider"], "deepseek")
        self.assertEqual(cfg["model"], "deepseek-chat")
        self.assertEqual(cfg["api_key"], "sk-test")

    def test_empty_model_falls_back_to_config_model(self):
        cfg = llm.lore_model_config({"provider": "ollama", "ollama_model": ""})
        self.assertEqual(cfg["model"], llm.config.MODEL_NAME)

    def test_config_has_no_analyze_model(self):
        self.assertFalse(hasattr(llm.config, "ANALYZE_MODEL_NAME"))


class ParseLoreJsonTests(unittest.TestCase):
    def test_plain_json(self):
        parsed = llm._parse_lore_json('{"Alice": "Тихая и наблюдательная."}')
        self.assertEqual(parsed, {"Alice": "Тихая и наблюдательная."})

    def test_fenced_json(self):
        raw = '```json\n{"Alice": "Мечтательница."}\n```'
        self.assertEqual(llm._parse_lore_json(raw), {"Alice": "Мечтательница."})

    def test_empty_object_means_no_character(self):
        self.assertEqual(llm._parse_lore_json("{}"), {})

    def test_garbage_returns_empty(self):
        self.assertEqual(llm._parse_lore_json("не JSON вообще"), {})
        self.assertEqual(llm._parse_lore_json(None), {})
        self.assertEqual(llm._parse_lore_json(""), {})

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(llm._parse_lore_json('["Alice"]'), {})

    def test_template_placeholder_rejected(self):
        # Модели иногда возвращают сам шаблон из промпта вместо описания.
        self.assertEqual(llm._parse_lore_json('{"Kaelor": "Описание на русском"}'), {})
        self.assertEqual(llm._parse_lore_json('{"Kaelor": "описание"}'), {})

    def test_too_short_description_rejected(self):
        self.assertEqual(llm._parse_lore_json('{"Kaelor": "Он"}'), {})

    def test_real_description_kept(self):
        raw = '{"Kaelor": "Говорит спокойно и уверенно, как лидер."}'
        self.assertEqual(
            llm._parse_lore_json(raw),
            {"Kaelor": "Говорит спокойно и уверенно, как лидер."},
        )

    def test_cjk_garbage_rejected(self):
        # qwen2.5:3b иногда смешивает китайские иероглифы с русским текстом.
        raw = '{"Kaelor": "Это вы听起来像是一个需要迅速行动的角色。"}'
        self.assertEqual(llm._parse_lore_json(raw), {})


class OllamaModelReadyTests(unittest.TestCase):
    def test_model_present(self):
        fake = FakeResponse({"models": [{"name": "qwen2.5:3b"}]})
        with mock.patch.object(llm.requests, "get", return_value=fake):
            ready, reason = llm._ollama_model_ready("qwen2.5:3b")
        self.assertTrue(ready)
        self.assertEqual(reason, "")

    def test_implicit_latest_tag(self):
        fake = FakeResponse({"models": [{"name": "qwen2.5:latest"}]})
        with mock.patch.object(llm.requests, "get", return_value=fake):
            self.assertTrue(llm._ollama_model_ready("qwen2.5")[0])

    def test_model_missing(self):
        fake = FakeResponse({"models": [{"name": "translategemma:4b"}]})
        with mock.patch.object(llm.requests, "get", return_value=fake):
            ready, reason = llm._ollama_model_ready("gemma2:9b")
        self.assertFalse(ready)
        self.assertIn("gemma2:9b", reason)
        self.assertIn("не найдена", reason)

    def test_service_down(self):
        with mock.patch.object(llm.requests, "get", side_effect=OSError("connection refused")):
            ready, reason = llm._ollama_model_ready("qwen2.5:3b")
        self.assertFalse(ready)
        self.assertIn("недоступна", reason)


class OllamaLoreCompletionTests(unittest.TestCase):
    """Лор идёт через общий провайдер, а не через нативный /api/generate."""

    def test_request_uses_selected_model(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        fake_provider = _fake_provider("Ollama", '{"Alice": "Загадочная."}')
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")) as ready, \
                mock.patch.object(llm, "make_provider", return_value=fake_provider) as factory:
            raw = llm._lore_completion("prompt", cfg)

        self.assertIn("Alice", raw)
        ready.assert_called_once_with("qwen2.5:3b")
        self.assertEqual(factory.call_args.args[0], "ollama")
        self.assertEqual(factory.call_args.kwargs["model"], "qwen2.5:3b")
        self.assertEqual(fake_provider.generate.call_args.args[0], "prompt")
        fake_provider.close.assert_called_once()

    def test_unavailable_model_skips_request(self):
        cfg = {"provider": "ollama", "model": "gemma2:9b", "api_key": ""}
        with mock.patch.object(
            llm, "_ollama_model_ready", return_value=(False, "модель «gemma2:9b» не найдена")
        ), mock.patch.object(llm, "make_provider") as factory:
            self.assertIsNone(llm._lore_completion("prompt", cfg))
        factory.assert_not_called()

    def test_http_error_returns_none(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        fake_provider = _fake_provider(
            "Ollama", side_effect=ProviderHTTPError("ollama", 500, "boom")
        )
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")), \
                mock.patch.object(llm, "make_provider", return_value=fake_provider):
            self.assertIsNone(llm._lore_completion("prompt", cfg))
        # Провайдер закрывается и на пути ошибки, а не только при успехе.
        fake_provider.close.assert_called_once()

    def test_connection_error_returns_none(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        fake_provider = _fake_provider("Ollama", side_effect=OSError("down"))
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")), \
                mock.patch.object(llm, "make_provider", return_value=fake_provider):
            self.assertIsNone(llm._lore_completion("prompt", cfg))


class DeepSeekLoreCompletionTests(unittest.TestCase):
    def test_uses_model_and_key(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake_provider = _fake_provider("DeepSeek", '{"Alice": "Описание."}')
        with mock.patch.object(llm, "make_provider", return_value=fake_provider) as factory:
            raw = llm._lore_completion("prompt", cfg)

        self.assertIn("Alice", raw)
        self.assertEqual(factory.call_args.args[0], "deepseek")
        self.assertEqual(factory.call_args.kwargs["model"], "deepseek-chat")
        # Ключ уходит в настройки провайдера, а не в сам HTTP-запрос.
        settings = factory.call_args.args[1]
        self.assertEqual(settings["api_key"], "sk-test")
        self.assertEqual(settings["deepseek_model"], "deepseek-chat")
        fake_provider.close.assert_called_once()

    def test_without_key_returns_none(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": ""}
        with mock.patch.object(llm, "make_provider") as factory:
            self.assertIsNone(llm._lore_completion("prompt", cfg))
        factory.assert_not_called()

    def test_http_error_returns_none(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake_provider = _fake_provider(
            "DeepSeek", side_effect=PermissionError("Неверный API-ключ для DeepSeek")
        )
        with mock.patch.object(llm, "make_provider", return_value=fake_provider):
            self.assertIsNone(llm._lore_completion("prompt", cfg))

    def test_429_returns_none_with_clear_message(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake_provider = _fake_provider(
            "DeepSeek", side_effect=RateLimitError("deepseek", 7)
        )
        with mock.patch.object(llm, "make_provider", return_value=fake_provider):
            with self.assertLogs(llm.log, level="WARNING") as captured:
                self.assertIsNone(llm._lore_completion("prompt", cfg))
        text = "\n".join(captured.output)
        self.assertIn("429", text)
        # Именно то число, которое прислал сервер (а не 30 по умолчанию).
        self.assertIn("7", text)
        self.assertIn("лимит", text)
        fake_provider.close.assert_called_once()

    def test_429_without_retry_after_does_not_invent_wait(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake_provider = _fake_provider(
            "DeepSeek", side_effect=RateLimitError("deepseek", None)
        )
        with mock.patch.object(llm, "make_provider", return_value=fake_provider):
            with self.assertLogs(llm.log, level="WARNING") as captured:
                self.assertIsNone(llm._lore_completion("prompt", cfg))
        text = "\n".join(captured.output)
        self.assertIn("429", text)
        # Не выдумываем «сервер просит подождать N с», если заголовка не было.
        self.assertNotIn("просит подождать", text)


class DeepSeekTranslationErrorTests(unittest.TestCase):
    def test_429_message_for_translation(self):
        provider = make_provider(
            "deepseek", {"api_key": "sk-test", "deepseek_model": "deepseek-chat"}
        )
        self.addCleanup(provider.close)
        with mock.patch.object(
            provider, "generate", side_effect=RateLimitError("deepseek", 30)
        ), mock.patch.object(
            llm, "_build_translation_prompt", return_value=("system", "user")
        ):
            result = llm.translate("hi", provider=provider)
        self.assertIn("429", result)
        self.assertIn("лимит", result)


class BackgroundAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}

        def _save(name, description=None, card=None):
            """Запоминаем описание — оно приходит либо аргументом, либо в карточке."""
            if description is None and isinstance(card, dict):
                description = card.get("description")
            self.saved[name] = description
            return True

        patcher = mock.patch.object(llm, "save_character", side_effect=_save)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_duckduckgo_hit_does_not_call_model(self):
        with mock.patch.object(
            llm.web_search, "fetch_character_lore", return_value="С вики."
        ), mock.patch.object(llm, "_lore_completion") as completion:
            llm.background_analyze("Alice", "Alice: hi", {"provider": "ollama", "model": "x"})
        completion.assert_not_called()
        self.assertEqual(self.saved, {"Alice": "С вики."})

    def test_duckduckgo_miss_uses_translation_model(self):
        cfg = {"provider": "ollama", "model": "translategemma:4b", "api_key": ""}
        with mock.patch.object(llm.web_search, "fetch_character_lore", return_value=""), \
                mock.patch.object(
                    llm, "_lore_completion", return_value='{"Alice": "Загадочная."}'
                ) as completion:
            llm.background_analyze("Alice", "Alice: hi", cfg)

        completion.assert_called_once()
        self.assertEqual(completion.call_args.args[1]["model"], "translategemma:4b")
        self.assertEqual(self.saved, {"Alice": "Загадочная."})

    def test_model_unavailable_saves_nothing(self):
        with mock.patch.object(llm.web_search, "fetch_character_lore", return_value=""), \
                mock.patch.object(llm, "_lore_completion", return_value=None):
            llm.background_analyze("Alice", "Alice: hi", {"provider": "ollama", "model": "x"})
        self.assertEqual(self.saved, {})

    def test_model_returns_empty_object_saves_nothing(self):
        with mock.patch.object(llm.web_search, "fetch_character_lore", return_value=""), \
                mock.patch.object(llm, "_lore_completion", return_value="{}"):
            llm.background_analyze("Alice", "Alice: hi", {"provider": "ollama", "model": "x"})
        self.assertEqual(self.saved, {})

    def test_web_search_exception_falls_back_to_model(self):
        with mock.patch.object(
            llm.web_search, "fetch_character_lore", side_effect=RuntimeError("нет сети")
        ), mock.patch.object(
            llm, "_lore_completion", return_value='{"Alice": "Из модели."}'
        ) as completion:
            llm.background_analyze("Alice", "Alice: hi", {"provider": "deepseek", "model": "deepseek-chat"})
        completion.assert_called_once()
        self.assertEqual(self.saved.get("Alice"), "Из модели.")


class AnalyzeCharacterTests(unittest.TestCase):
    def setUp(self):
        _clear_lore_state()

    def tearDown(self):
        _clear_lore_state()

    def _fake_thread(self, captured):
        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kwargs):
                captured["target"] = target
                captured["args"] = args

            def start(self):
                captured["started"] = True

        return FakeThread

    def test_passes_settings_to_background_analysis(self):
        captured = {}
        settings = {
            "provider": "deepseek",
            "deepseek_model": "deepseek-chat",
            "api_key": "sk-test",
        }
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(captured)):
            llm.analyze_character("Alice: hello there", settings)

        self.assertTrue(captured.get("started"))
        self.assertIs(captured["target"], llm.background_analyze)
        self.assertEqual(captured["args"][0], "Alice")
        self.assertEqual(captured["args"][2]["provider"], "deepseek")
        self.assertEqual(captured["args"][2]["model"], "deepseek-chat")

    def test_known_character_is_skipped(self):
        with mock.patch.object(llm, "load_characters", return_value={"Alice": "уже есть"}), \
                mock.patch.object(llm.threading, "Thread") as thread:
            llm.analyze_character("Alice: hello", {"provider": "ollama"})
        thread.assert_not_called()

    def test_without_settings_falls_back_to_load_settings(self):
        captured = {}
        settings = {"provider": "ollama", "ollama_model": "qwen2.5:3b", "api_key": ""}
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm, "load_settings", return_value=settings), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(captured)):
            llm.analyze_character("Bob: hi")
        self.assertEqual(captured["args"][2]["model"], "qwen2.5:3b")

    def test_garbage_name_is_skipped(self):
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm.threading, "Thread") as thread:
            llm.analyze_character("Surprisingly: yes", {"provider": "ollama"})
        thread.assert_not_called()

    def test_internal_voice_uses_me(self):
        captured = {}
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(captured)):
            llm.analyze_character("Me: I should go", {"provider": "ollama"})
        self.assertEqual(captured["args"][0], "Я")


class LoreDeduplicationTests(unittest.TestCase):
    """Один персонаж не должен анализироваться на каждый кадр."""

    def setUp(self):
        _clear_lore_state()

    def tearDown(self):
        _clear_lore_state()

    def _fake_thread(self, started):
        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kwargs):
                self.args = args

            def start(self):
                started.append(self.args[0])

        return FakeThread

    def test_second_encounter_while_in_flight_is_ignored(self):
        started = []
        settings = {"provider": "ollama", "ollama_model": "qwen2.5:3b"}
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(started)):
            for _ in range(10):
                llm.analyze_character("Alice: hello", settings)
        self.assertEqual(started, ["Alice"])

    def test_retry_allowed_after_ttl(self):
        started = []
        llm._LORE_LAST_ATTEMPT["Alice"] = time.monotonic() - llm._LORE_RETRY_SECONDS - 5
        with mock.patch.object(llm, "load_characters", return_value={}), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(started)):
            llm.analyze_character("Alice: hello", {"provider": "ollama", "ollama_model": "qwen2.5:3b"})
        self.assertEqual(started, ["Alice"])

    def test_saved_character_not_analyzed_again(self):
        started = []
        with mock.patch.object(llm, "load_characters", return_value={"Alice": "уже есть"}), \
                mock.patch.object(llm.threading, "Thread", self._fake_thread(started)):
            llm.analyze_character("Alice: hello", {"provider": "ollama", "ollama_model": "qwen2.5:3b"})
        self.assertEqual(started, [])

    def test_background_analyze_releases_in_flight(self):
        llm._LORE_INFLIGHT.add("Alice")
        with mock.patch.object(llm.web_search, "fetch_character_lore", return_value=""), \
                mock.patch.object(llm, "_lore_completion", return_value=None), \
                mock.patch.object(llm, "save_character"):
            llm.background_analyze("Alice", "Alice: hi", {"provider": "ollama", "model": "x"})
        self.assertNotIn("Alice", llm._LORE_INFLIGHT)
        self.assertIn("Alice", llm._LORE_LAST_ATTEMPT)


class LegacyConfigMigrationTests(unittest.TestCase):
    """Старый config.py чистится сам, а не просто предупреждает каждый запуск."""

    def _write_config(self, tmp, text):
        path = os.path.join(tmp, "config.py")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)
        return path

    def test_removes_constant_and_creates_backup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            original = (
                'MODEL_NAME = "translategemma:4b"\n'
                'ANALYZE_MODEL_NAME = "gemma2:9b"\n'
                "ENABLE_WEB_SEARCH = True\n"
            )
            path = self._write_config(tmp, original)

            self.assertTrue(llm._migrate_legacy_config(path))

            with open(path, encoding="utf-8") as stream:
                updated = stream.read()
            self.assertNotIn("ANALYZE_MODEL_NAME", updated)
            self.assertIn("ENABLE_WEB_SEARCH = True", updated)
            self.assertIn("MODEL_NAME", updated)

            backups = [name for name in os.listdir(tmp) if name.startswith("config.py.bak-")]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(tmp, backups[0]), encoding="utf-8") as stream:
                self.assertIn("ANALYZE_MODEL_NAME", stream.read())

    def test_no_legacy_constant_is_noop(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, 'MODEL_NAME = "x"\n')
            self.assertFalse(llm._migrate_legacy_config(path))
            self.assertEqual(len(os.listdir(tmp)), 1)  # бэкап не создаётся

    def test_missing_file_is_noop(self):
        self.assertFalse(llm._migrate_legacy_config(os.path.join("no", "such", "config.py")))

    def test_invalid_python_is_not_written(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # После удаления строки останется битый Python — файл трогать нельзя.
            path = self._write_config(tmp, 'ANALYZE_MODEL_NAME = "gemma2:9b"\nif True:\n')
            with open(path, encoding="utf-8") as stream:
                before = stream.read()

            self.assertFalse(llm._migrate_legacy_config(path))

            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), before)


class LoreTimingLogTests(unittest.TestCase):
    def test_logs_model_and_elapsed_time(self):
        with mock.patch.object(llm.web_search, "fetch_character_lore", return_value=""), \
                mock.patch.object(
                    llm, "_lore_completion", return_value='{"Alice": "Спокойная и уверенная."}'
                ), mock.patch.object(llm, "save_character"), \
                self.assertLogs(llm.log, level="INFO") as captured:
            llm.background_analyze(
                "Alice", "Alice: hi", {"provider": "ollama", "model": "qwen2.5:3b"}
            )
        text = "\n".join(captured.output)
        self.assertIn("qwen2.5:3b", text)
        self.assertIn("всего", text)
        self.assertIn("с)", text)


if __name__ == "__main__":
    unittest.main()
