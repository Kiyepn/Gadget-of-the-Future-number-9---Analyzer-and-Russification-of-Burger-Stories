# tests/test_launcher.py
"""Тесты чистой логики лаунчера (v1.5.1).

Окно здесь не создаётся: проверяются только модульные хелперы, которые
используются в UI (темы, сравнение имён моделей Ollama, форматирование
объёма, путь к рабочему столу). Сам UI проверяется скриншотами вручную.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui_launcher as launcher  # noqa: E402


class ThemeTests(unittest.TestCase):
    def tearDown(self):
        launcher.apply_theme("violet")

    def test_unknown_theme_falls_back_to_violet(self):
        self.assertEqual(launcher.apply_theme("tesseract"), "violet")
        self.assertEqual(launcher.apply_theme(""), "violet")

    def test_theme_switches_accent_colors(self):
        launcher.apply_theme("blue")
        blue_accent = launcher.ACCENT
        launcher.apply_theme("green")
        self.assertNotEqual(blue_accent, launcher.ACCENT)
        self.assertEqual(launcher.ACCENT, launcher.THEMES["green"]["accent"])

    def test_all_declared_themes_exist(self):
        for name in launcher.THEME_LABELS.values():
            self.assertIn(name, launcher.THEMES)


class ModelNameTests(unittest.TestCase):
    def test_explicit_tag(self):
        self.assertEqual(launcher.normalize_model_name("translategemma:4b"), ("translategemma", "4b"))

    def test_implicit_latest(self):
        self.assertEqual(launcher.normalize_model_name("qwen2.5"), ("qwen2.5", "latest"))

    def test_matching_with_and_without_tag(self):
        self.assertTrue(launcher.model_names_match("qwen2.5", "qwen2.5:latest"))
        self.assertTrue(launcher.model_names_match("QWEN2.5:3B", "qwen2.5:3b"))
        self.assertFalse(launcher.model_names_match("qwen2.5:3b", "qwen2.5:7b"))
        self.assertFalse(launcher.model_names_match("", "qwen2.5:3b"))


class FormatBytesTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(launcher.format_bytes(0), "0 Б")
        self.assertEqual(launcher.format_bytes(1536), "2 КБ")
        self.assertEqual(launcher.format_bytes(5 * 1024 ** 2), "5 МБ")
        self.assertEqual(launcher.format_bytes(3.3 * 1024 ** 3), "3.30 ГБ")

    def test_garbage_does_not_crash(self):
        self.assertEqual(launcher.format_bytes("abc"), "?")
        self.assertEqual(launcher.format_bytes(None), "?")


class DesktopTests(unittest.TestCase):
    def test_desktop_dir_exists(self):
        path = launcher.desktop_dir()
        self.assertTrue(os.path.isdir(path), path)

    def test_shortcut_path_is_lnk(self):
        app = type("Stub", (), {})()
        path = os.path.join(launcher.desktop_dir(), "Gadget Test Shortcut.lnk")
        self.assertTrue(path.endswith(".lnk"))


if __name__ == "__main__":
    unittest.main()
