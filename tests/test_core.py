# tests/test_core.py
"""Тесты путей, настроек и памяти (без GUI и без нейросетей).

Запуск:  python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brain import paths as paths_mod  # noqa: E402
from brain import settings as settings_mod  # noqa: E402
from brain import memory_engine  # noqa: E402


class TempDataDirCase(unittest.TestCase):
    """Каждый тест работает в своей временной папке data/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gadget_test_")
        self._old_env = os.environ.get("GADGET_DATA_DIR")
        os.environ["GADGET_DATA_DIR"] = self.tmp
        paths_mod.resolve_data_dir(force=True)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("GADGET_DATA_DIR", None)
        else:
            os.environ["GADGET_DATA_DIR"] = self._old_env
        paths_mod.resolve_data_dir(force=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


class PathsTests(TempDataDirCase):
    def test_override_is_respected(self):
        self.assertEqual(
            os.path.normcase(paths_mod.data_dir()), os.path.normcase(self.tmp)
        )

    def test_data_file_creates_dir(self):
        path = paths_mod.data_file("x.json")
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_data_location_hint_mentions_path(self):
        self.assertIn(paths_mod.data_dir(), paths_mod.data_location_hint())

    def test_data_dir_is_stable(self):
        self.assertEqual(paths_mod.data_dir(), paths_mod.data_dir())


class SettingsTests(TempDataDirCase):
    def test_defaults_and_types(self):
        settings = settings_mod.load_settings()
        self.assertEqual(settings["provider"], "ollama")
        self.assertIsInstance(settings["overlay_enabled"], bool)
        self.assertGreaterEqual(settings["overlay_opacity"], 0.3)
        self.assertIn(settings["overlay_position"], settings_mod.OVERLAY_POSITIONS)

    def test_sanitize_clamps_values(self):
        saved = settings_mod.save_settings({
            "overlay_opacity": 5,
            "overlay_font_size": 999,
            "overlay_width": 1,
            "overlay_duration": -3,
            "overlay_position": "somewhere",
            "overlay_accent": "rainbow",
            "provider": "unknown",
            "ocr_gpu": "quantum",
        })
        self.assertEqual(saved["overlay_opacity"], 1.0)
        self.assertEqual(saved["overlay_font_size"], 40)
        self.assertEqual(saved["overlay_width"], 320)
        self.assertEqual(saved["overlay_duration"], 2.0)
        self.assertEqual(saved["overlay_position"], "bottom-center")
        self.assertEqual(saved["overlay_accent"], "violet")
        self.assertEqual(saved["provider"], "ollama")
        self.assertEqual(saved["ocr_gpu"], "auto")

    def test_api_key_is_encrypted_at_rest(self):
        settings_mod.save_settings({"api_key": "sk-secret-123"})
        with open(settings_mod.settings_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertNotEqual(raw["api_key"], "sk-secret-123")
        if raw["api_key"]:
            self.assertTrue(raw["api_key"].startswith("dpapi:"))

    def test_api_key_roundtrip(self):
        settings_mod.save_settings({"api_key": "sk-roundtrip-проверка"})
        self.assertEqual(
            settings_mod.load_settings()["api_key"], "sk-roundtrip-проверка"
        )

    def test_legacy_plaintext_key_is_read(self):
        path = settings_mod.settings_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"api_key": "sk-plain-old"}, f)
        self.assertEqual(settings_mod.load_settings()["api_key"], "sk-plain-old")

    def test_broken_json_does_not_crash(self):
        with open(settings_mod.settings_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(settings_mod.load_settings()["provider"], "ollama")

    def test_env_overrides_file(self):
        settings_mod.save_settings({"provider": "ollama"})
        old = os.environ.get("GADGET_PROVIDER")
        os.environ["GADGET_PROVIDER"] = "deepseek"
        try:
            self.assertEqual(settings_mod.load_settings()["provider"], "deepseek")
        finally:
            if old is None:
                os.environ.pop("GADGET_PROVIDER", None)
            else:
                os.environ["GADGET_PROVIDER"] = old


class MemoryTests(TempDataDirCase):
    def test_save_and_load(self):
        memory_engine.clear_memory()
        self.assertTrue(memory_engine.save_memory("Hello", "Привет"))
        self.assertEqual(memory_engine.load_memory(),
                         [{"original": "Hello", "translated": "Привет"}])

    def test_memory_is_capped(self):
        memory_engine.clear_memory()
        for i in range(memory_engine.MAX_MEMORY + 10):
            memory_engine.save_memory(f"line {i}", f"строка {i}")
        memory = memory_engine.load_memory()
        self.assertEqual(len(memory), memory_engine.MAX_MEMORY)
        self.assertIn("line 24", memory[-1]["original"])

    def test_clear_memory(self):
        memory_engine.save_memory("a", "б")
        memory_engine.clear_memory()
        self.assertEqual(memory_engine.load_memory(), [])

    def test_corrupt_file_is_quarantined(self):
        memory_engine.clear_memory()
        with open(memory_engine.memory_path(), "w", encoding="utf-8") as f:
            f.write("[{broken")
        self.assertEqual(memory_engine.load_memory(), [])
        corrupt = [
            name for name in os.listdir(self.tmp)
            if name.startswith("memory.json.corrupt-")
        ]
        self.assertTrue(corrupt, "битый файл должен сохраняться для разбора")
        # После этого запись снова работает.
        self.assertTrue(memory_engine.save_memory("x", "у"))

    def test_character_saved_once(self):
        self.assertTrue(memory_engine.save_character("Robert", "Механик"))
        self.assertFalse(memory_engine.save_character("Robert", "Другое"))
        card = memory_engine.load_characters()["Robert"]
        # В 1.6.0 значение — карточка, а не строка.
        self.assertEqual(card["description"], "Механик")
        self.assertEqual(card["gender"], "unknown")
        self.assertEqual(card["address"], "ty")

    def test_prompt_format_uses_saved_memory(self):
        memory_engine.clear_memory()
        memory_engine.save_memory("Hi", "Привет")
        prompt = memory_engine.format_memory_for_prompt()
        self.assertIn("Hi", prompt)
        self.assertIn("Привет", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
