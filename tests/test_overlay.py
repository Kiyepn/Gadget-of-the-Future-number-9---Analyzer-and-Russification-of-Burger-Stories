# tests/test_overlay.py
"""Тесты оверлея: реальное окно Tk, Win32-стили и режим --overlay-test.

Тесты требуют графическую сессию Windows, поэтому на других ОС они
пропускаются: в CI без дисплея Tk не поднимется.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brain import paths as paths_mod  # noqa: E402

WINDOWS = sys.platform == "win32"


def _have_tk():
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(WINDOWS, "оверлей использует Win32 API")
@unittest.skipUnless(_have_tk(), "нет графической сессии для tkinter")
class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from brain.overlay import Overlay

        cls.Overlay = Overlay

    def setUp(self):
        self.overlay = None
        self.tmp = tempfile.mkdtemp(prefix="gadget_overlay_")
        self._old_env = os.environ.get("GADGET_DATA_DIR")
        os.environ["GADGET_DATA_DIR"] = self.tmp
        paths_mod.resolve_data_dir(force=True)

    def tearDown(self):
        if self.overlay is not None:
            self.overlay.stop()
        if self._old_env is None:
            os.environ.pop("GADGET_DATA_DIR", None)
        else:
            os.environ["GADGET_DATA_DIR"] = self._old_env
        paths_mod.resolve_data_dir(force=True)

    def _start(self, **settings):
        overlay = self.Overlay(settings)
        self.assertTrue(overlay.start(timeout=10.0), "оверлей не стартовал")
        self.overlay = overlay
        return overlay

    def test_window_has_required_styles(self):
        overlay = self._start(overlay_duration=30)
        overlay.show_translation("Hello world", "Привет, мир")
        info = overlay.debug_hwnd_info()
        self.assertTrue(info["running"])
        self.assertTrue(info["visible"], "окно должно быть показано")
        self.assertTrue(info["layered"], "нет WS_EX_LAYERED")
        self.assertTrue(info["click_through"], "нет WS_EX_TRANSPARENT")
        self.assertTrue(info["toolwindow"], "нет WS_EX_TOOLWINDOW")
        self.assertTrue(info["noactivate"], "нет WS_EX_NOACTIVATE")
        self.assertTrue(info["topmost"], "окно не topmost")
        self.assertIn("x", info["geometry"])

    def test_overlay_is_above_active_window_and_click_through(self):
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.GetWindow.restype = wt.HWND
        user32.GetTopWindow.restype = wt.HWND
        user32.WindowFromPoint.restype = wt.HWND

        overlay = self._start(overlay_position="center", overlay_duration=30)
        overlay.show_translation("Line", "Строка")
        info = overlay.debug_hwnd_info()

        def z_order():
            result = []
            hwnd = user32.GetTopWindow(None)
            while hwnd:
                if user32.IsWindowVisible(hwnd):
                    result.append(hwnd)
                hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
            return result

        order = z_order()
        self.assertIn(info["parent"], order, "окно оверлея не в z-порядке")
        z_index = order.index(info["parent"])
        self.assertLess(z_index, 8, "оверлей должен быть в верхней части z-порядка")

        rect = wt.RECT()
        user32.GetWindowRect(wt.HWND(info["parent"]), ctypes.byref(rect))
        center = wt.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        hit = user32.WindowFromPoint(center)
        # Клики должны проходить сквозь оверлей к окну под ним.
        self.assertNotEqual(hit, info["parent"], "клик перехватывается оверлеем")
        self.assertNotEqual(hit, info["hwnd"], "клик перехватывается оверлеем")

    def test_status_card_and_toggle(self):
        overlay = self._start(overlay_duration=30)
        overlay.show_status("Пауза", "info", duration=0)
        info = overlay.debug_hwnd_info()
        self.assertTrue(info["visible"])
        self.assertIn("Пауза", info["last_text"])
        overlay.toggle_visible()
        self.assertFalse(overlay.debug_hwnd_info()["visible"])
        overlay.toggle_visible()
        self.assertTrue(overlay.debug_hwnd_info()["visible"])

    def test_invalid_settings_fall_back_to_defaults(self):
        overlay = self._start(
            overlay_position="на луне",
            overlay_accent="радуга",
            overlay_opacity=42,
            overlay_font_size=-5,
            overlay_duration=0,
        )
        info = overlay.debug_hwnd_info()
        self.assertEqual(info["position"], "bottom-center")
        self.assertLessEqual(info["opacity"], 1.0)
        self.assertGreaterEqual(info["opacity"], 0.3)

    def test_stop_is_idempotent(self):
        overlay = self._start()
        overlay.show_translation("a", "б")
        overlay.stop()
        self.assertFalse(overlay.is_running)
        overlay.stop()  # повторная остановка не должна падать
        self.assertFalse(overlay.is_running)


@unittest.skipUnless(WINDOWS, "оверлей использует Win32 API")
@unittest.skipUnless(_have_tk(), "нет графической сессии для tkinter")
class OverlayTestModeTests(unittest.TestCase):
    def test_overlay_test_cli_exits_zero(self):
        tmp = tempfile.mkdtemp(prefix="gadget_cli_")
        env = dict(os.environ)
        env["GADGET_DATA_DIR"] = tmp
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "main.py"),
             "--overlay-test", "--seconds", "1"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertTrue(
            os.path.exists(os.path.join(tmp, "gadget.log")),
            "журнал должен создаваться в папке данных",
        )
        with open(os.path.join(tmp, "gadget.log"), "r", encoding="utf-8") as f:
            self.assertIn("1.4.2", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
