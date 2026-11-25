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
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.llm_engine as llm  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


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
    def test_request_uses_selected_model(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        fake = FakeResponse({"response": '{"Alice": "Загадочная."}'})
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")) as ready, \
                mock.patch.object(llm.requests, "post", return_value=fake) as post:
            raw = llm._lore_completion("prompt", cfg)

        self.assertIn("Alice", raw)
        ready.assert_called_once_with("qwen2.5:3b")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen2.5:3b")
        self.assertNotEqual(payload["model"], "gemma2:9b")

    def test_unavailable_model_skips_request(self):
        cfg = {"provider": "ollama", "model": "gemma2:9b", "api_key": ""}
        with mock.patch.object(
            llm, "_ollama_model_ready", return_value=(False, "модель «gemma2:9b» не найдена")
        ), mock.patch.object(llm.requests, "post") as post:
            self.assertIsNone(llm._lore_completion("prompt", cfg))
        post.assert_not_called()

    def test_http_error_returns_none(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")), \
                mock.patch.object(llm.requests, "post", return_value=FakeResponse({"error": "boom"})):
            self.assertIsNone(llm._lore_completion("prompt", cfg))

    def test_connection_error_returns_none(self):
        cfg = {"provider": "ollama", "model": "qwen2.5:3b", "api_key": ""}
        with mock.patch.object(llm, "_ollama_model_ready", return_value=(True, "")), \
                mock.patch.object(llm.requests, "post", side_effect=OSError("down")):
            self.assertIsNone(llm._lore_completion("prompt", cfg))


class DeepSeekLoreCompletionTests(unittest.TestCase):
    def test_uses_model_and_key(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake = FakeResponse({"choices": [{"message": {"content": '{"Alice": "Описание."}'}}]})
        with mock.patch.object(llm.requests, "post", return_value=fake) as post:
            raw = llm._lore_completion("prompt", cfg)

        self.assertIn("Alice", raw)
        args = post.call_args
        self.assertEqual(args.args[0], llm.DEEPSEEK_URL)
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(args.kwargs["json"]["model"], "deepseek-chat")

    def test_without_key_returns_none(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": ""}
        with mock.patch.object(llm.requests, "post") as post:
            self.assertIsNone(llm._lore_completion("prompt", cfg))
        post.assert_not_called()

    def test_http_error_returns_none(self):
        cfg = {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"}
        fake = FakeResponse({"error": {"message": "Authentication Fails"}}, status_code=401)
        with mock.patch.object(llm.requests, "post", return_value=fake):
            self.assertIsNone(llm._lore_completion("prompt", cfg))


class BackgroundAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        patcher = mock.patch.object(
            llm, "save_character",
            side_effect=lambda name, desc: self.saved.__setitem__(name, desc),
        )
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


if __name__ == "__main__":
    unittest.main()
