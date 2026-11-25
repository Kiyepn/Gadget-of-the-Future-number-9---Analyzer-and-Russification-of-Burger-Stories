# tests/test_launcher.py
"""Тесты чистой логики лаунчера (v1.5.1).

Окно здесь не создаётся: проверяются только модульные хелперы, которые
используются в UI (темы, сравнение имён моделей Ollama, форматирование
объёма, путь к рабочему столу). Сам UI проверяется скриншотами вручную.
"""

import os
import sys
import unittest
from unittest import mock

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

    def test_speed_units(self):
        self.assertEqual(launcher.format_speed(0), "0 КБ/с")
        self.assertEqual(launcher.format_speed(1024 ** 2 * 12.34), "12.3 МБ/с")
        self.assertEqual(launcher.format_speed(800 * 1024), "800 КБ/с")

    def test_duration_units(self):
        self.assertEqual(launcher.format_duration(0.3), "меньше секунды")
        self.assertEqual(launcher.format_duration(15), "~15 с")
        self.assertEqual(launcher.format_duration(125), "~2 мин 05 с")
        self.assertEqual(launcher.format_duration(3900), "~1 ч 05 мин")


class DownloadTrackerTests(unittest.TestCase):
    """Агрегация прогресса по слоям Ollama: процент, скорость, ETA."""

    def test_single_layer_progress(self):
        tracker = launcher.DownloadTracker()
        info = tracker.update(digest="a", completed=50, total=100, now=0.0)
        self.assertEqual(info["fraction"], 0.5)
        self.assertEqual(info["completed"], 50)
        self.assertEqual(info["total"], 100)
        self.assertIsNone(info["eta"])  # без истории скорость ещё неизвестна

    def test_multiple_layers_are_summed(self):
        tracker = launcher.DownloadTracker()
        tracker.update(digest="a", completed=100, total=100, now=0.0)
        info = tracker.update(digest="b", completed=0, total=300, now=1.0)
        self.assertEqual(info["total"], 400)
        self.assertEqual(info["completed"], 100)
        self.assertAlmostEqual(info["fraction"], 0.25)

    def test_speed_and_eta_after_steady_progress(self):
        tracker = launcher.DownloadTracker()
        # 10 МБ/с: за 5 секунд прошли 50 МБ из 100 МБ
        for second in range(6):
            info = tracker.update(
                digest="a", completed=second * 10 * 1024 ** 2,
                total=100 * 1024 ** 2, now=float(second),
            )
        self.assertAlmostEqual(info["fraction"], 0.5, places=3)
        self.assertAlmostEqual(info["speed"] / (1024 ** 2), 10.0, places=1)
        self.assertAlmostEqual(info["eta"], 5.0, places=1)

    def test_no_total_means_indeterminate(self):
        tracker = launcher.DownloadTracker()
        info = tracker.update(digest=None, completed=None, total=None, now=0.0)
        self.assertIsNone(info["fraction"])
        self.assertIsNone(info["eta"])

    def test_eta_absent_when_speed_zero(self):
        tracker = launcher.DownloadTracker()
        tracker.update(digest="a", completed=0, total=100, now=0.0)
        info = tracker.update(digest="a", completed=0, total=100, now=3.0)
        self.assertEqual(info["speed"], 0.0)
        self.assertIsNone(info["eta"])


class DesktopTests(unittest.TestCase):
    def test_desktop_dir_exists(self):
        path = launcher.desktop_dir()
        self.assertTrue(os.path.isdir(path), path)

    def test_shortcut_path_is_lnk(self):
        app = type("Stub", (), {})()
        path = os.path.join(launcher.desktop_dir(), "Gadget Test Shortcut.lnk")
        self.assertTrue(path.endswith(".lnk"))


class ThemeUnificationTests(unittest.TestCase):
    """Тема лаунчера и цвет карточки перевода — одна настройка."""

    def test_theme_sets_overlay_accent(self):
        from brain.settings import _sanitize

        data = _sanitize({"launcher_theme": "mono", "overlay_accent": "violet"})
        self.assertEqual(data["launcher_theme"], "mono")
        self.assertEqual(data["overlay_accent"], "mono")

    def test_legacy_overlay_accent_becomes_theme(self):
        from brain.settings import _sanitize

        data = _sanitize({"launcher_theme": "", "overlay_accent": "orange"})
        self.assertEqual(data["launcher_theme"], "orange")
        self.assertEqual(data["overlay_accent"], "orange")

    def test_unknown_theme_falls_back_and_stays_in_sync(self):
        from brain.settings import _sanitize

        data = _sanitize({"launcher_theme": "tesseract"})
        self.assertEqual(data["launcher_theme"], "violet")
        self.assertEqual(data["overlay_accent"], "violet")

    def test_new_accents_exist_in_overlay_palette(self):
        from brain.overlay import ACCENTS

        for name in ("crimson", "mono"):
            self.assertIn(name, ACCENTS)


class FakeButton:
    def __init__(self):
        self.state = "normal"

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


class FakeProcess:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class RunButtonsSyncTests(unittest.TestCase):
    """После смены темы кнопки должны отражать реально запущенный переводчик."""

    def _fake_launcher(self, running, testing=False, pending=False):
        stub = type("Stub", (), {})()
        stub.process = FakeProcess(alive=running) if running else None
        stub._test_process = FakeProcess(alive=True) if testing else None
        stub._launch_pending = pending
        stub.launch_btn = FakeButton()
        stub.stop_btn = FakeButton()
        stub.test_overlay_btn = FakeButton()
        stub.status_text = None
        stub._set_status = lambda text, kind="info": setattr(stub, "status_text", text)
        return stub

    def test_running_translator_enables_stop(self):
        stub = self._fake_launcher(running=True)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.launch_btn.state, "disabled")
        self.assertEqual(stub.stop_btn.state, "normal")
        self.assertIn("уже запущен", stub.status_text)

    def test_stopped_translator_enables_launch(self):
        stub = self._fake_launcher(running=False)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.launch_btn.state, "normal")
        self.assertEqual(stub.stop_btn.state, "disabled")
        self.assertIsNone(stub.status_text)

    def test_pending_launch_disables_start_and_allows_stop(self):
        # Гонка: тема сменилась, пока шла проверка модели — процесса ещё нет.
        stub = self._fake_launcher(running=False, pending=True)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.launch_btn.state, "disabled")
        self.assertEqual(stub.stop_btn.state, "normal")
        self.assertIn("Запускаю", stub.status_text)

    def test_running_wins_over_pending(self):
        stub = self._fake_launcher(running=True, pending=True)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.stop_btn.state, "normal")
        self.assertIn("уже запущен", stub.status_text)

    def test_running_overlay_test_disables_button(self):
        stub = self._fake_launcher(running=False, testing=True)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.test_overlay_btn.state, "disabled")

    def test_finished_process_counts_as_stopped(self):
        stub = self._fake_launcher(running=True)
        stub.process = FakeProcess(alive=False)
        launcher.TranslatorLauncher._sync_run_buttons(stub)
        self.assertEqual(stub.launch_btn.state, "normal")
        self.assertEqual(stub.stop_btn.state, "disabled")


class LauncherGuiTests(unittest.TestCase):
    """Проверки на реальном окне: скролл и кнопки при смене темы."""

    def setUp(self):
        try:
            self.app = launcher.TranslatorLauncher()
        except Exception as exc:  # pragma: no cover - зависит от окружения
            self.skipTest(f"нет GUI: {exc}")
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        launcher.apply_theme("violet")

    def test_theme_change_keeps_scroll_position(self):
        canvas = self.app.body._parent_canvas
        canvas.yview_moveto(0.5)
        self.app.update()
        before = canvas.yview()[0]
        self.assertGreater(before, 0.2)  # прокрутка реально ушла вниз

        self.app.settings["launcher_theme"] = "blue"
        self.app._rebuild_ui()
        self.app.update()

        after = self.app.body._parent_canvas.yview()[0]
        self.assertAlmostEqual(before, after, delta=0.05)

    def test_theme_change_during_pending_launch_keeps_buttons_sane(self):
        # Имитируем «модель ещё проверяется»: процесса нет, запуск в процессе.
        self.app.process = None
        self.app._launch_pending = True
        self.app.settings["launcher_theme"] = "blue"
        self.app._rebuild_ui()
        self.app.update()

        self.assertEqual(self.app.launch_btn.cget("state"), "disabled")
        self.assertEqual(self.app.stop_btn.cget("state"), "normal")

        self.app.stop()
        self.assertFalse(self.app._launch_pending)
        self.assertEqual(self.app.launch_btn.cget("state"), "normal")
        self.assertEqual(self.app.stop_btn.cget("state"), "disabled")


class OverlayAccentTests(unittest.TestCase):
    """Статусы оверлея красятся темой; красный остаётся только для ошибок."""

    def _overlay(self, mode="status", kind="ok", accent="orange"):
        from brain.overlay import Overlay

        overlay = object.__new__(Overlay)
        overlay._mode = mode
        overlay._status_kind = kind
        overlay._accent = accent
        return overlay

    def test_ok_status_uses_theme_accent(self):
        from brain.overlay import ACCENTS

        self.assertEqual(self._overlay(kind="ok", accent="orange")._accent_color(), ACCENTS["orange"])
        self.assertEqual(self._overlay(kind="ok", accent="mono")._accent_color(), ACCENTS["mono"])

    def test_error_status_stays_red(self):
        from brain.overlay import STATUS_ERROR

        self.assertEqual(self._overlay(kind="error")._accent_color(), STATUS_ERROR)

    def test_translation_uses_theme_accent(self):
        from brain.overlay import ACCENTS

        self.assertEqual(
            self._overlay(mode="translation", accent="green")._accent_color(),
            ACCENTS["green"],
        )


class OverlayTestUsesSettingsTests(unittest.TestCase):
    """Режим --overlay-test должен брать тему из настроек, а не violet."""

    def test_overlay_test_passes_settings(self):
        import main as main_module

        captured = {}

        class FakeOverlay:
            def __init__(self, settings=None):
                captured["settings"] = settings

            def start(self):
                return True

            def show_translation(self, *args, **kwargs):
                captured["shown"] = True

            def stop(self):
                captured["stopped"] = True

        settings = {"overlay_accent": "orange", "launcher_theme": "orange"}
        with mock.patch.object(main_module, "Overlay", FakeOverlay), \
                mock.patch.object(main_module, "load_settings", return_value=settings), \
                mock.patch.object(main_module, "_guard_std_streams"), \
                mock.patch.object(main_module, "_enable_dpi_awareness"), \
                mock.patch.object(main_module, "setup_logging"), \
                mock.patch.object(main_module.time, "sleep"):
            code = main_module._run_overlay_test(0.01)

        self.assertEqual(code, 0)
        self.assertEqual(captured["settings"], settings)
        self.assertTrue(captured.get("shown"))
        self.assertTrue(captured.get("stopped"))


if __name__ == "__main__":
    unittest.main()
