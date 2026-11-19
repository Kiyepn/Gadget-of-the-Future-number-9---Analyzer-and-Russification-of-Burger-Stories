# tests/test_win_ocr.py
"""Тесты Windows Native OCR (brain/ocr_providers/win_ocr.py).

Проверяем главное обещание v1.5.0: распознавание работает без easyocr и
torch, а интерфейс совместим с EasyOCR (readtext/detail), поэтому main.py
переписывать не пришлось.

Тесты делятся на две группы:
* «живые» — реально дергают Windows.Media.Ocr (пропускаются, если в системе
  нет языкового пакета OCR);
* «юнит» — проверяют логику без движка: подготовку картинки, деградацию
  бэкендов, тайминги/ошибки. Они работают всегда.
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from brain.ocr_providers import win_ocr  # noqa: E402
from brain.ocr_providers.win_ocr import OcrUnavailableError, WinOCR  # noqa: E402

_FONT_CANDIDATES = ("arial.ttf", "segoeui.ttf", "tahoma.ttf")

# Доступность OCR проверяем один раз: is_available() может запускать
# powershell.exe (до 20 с), а skipUnless в декораторе вызвался бы при импорте.
_LIVE_AVAILABLE = None
_LIVE_LOCK = threading.Lock()


def live_available():
    global _LIVE_AVAILABLE
    if _LIVE_AVAILABLE is None:
        with _LIVE_LOCK:
            if _LIVE_AVAILABLE is None:
                _LIVE_AVAILABLE = WinOCR.is_available()
    return _LIVE_AVAILABLE


def _font(size):
    from PIL import ImageFont

    root = os.environ.get("SystemRoot", r"C:\Windows")
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(os.path.join(root, "Fonts", name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_image(lines, font_px=16):
    from PIL import ImageDraw

    img = Image.new("RGB", (720, 400), (18, 18, 24))
    draw = ImageDraw.Draw(img)
    font = _font(font_px)
    y = 40
    for line in lines:
        draw.text((30, y), line, fill=(240, 240, 245), font=font)
        y += font_px + 14
    return img


class WinOCRUnitTests(unittest.TestCase):
    """Логика без запуска движка — работает на любой машине."""

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            WinOCR(backend="tesseract")

    def test_prepare_image_upscales_small_frames(self):
        original = Image.new("RGB", (320, 100), "white")
        prepared = win_ocr._prepare_image(original, upscale=True)
        self.assertGreater(max(prepared.size), max(original.size))
        # Больше чем в 2 раза мелкий кадр не растягиваем — это лишняя работа.
        self.assertLessEqual(max(prepared.size), max(original.size) * 2)

    def test_prepare_image_upscale_can_be_disabled(self):
        prepared = win_ocr._prepare_image(Image.new("RGB", (320, 100), "white"), upscale=False)
        self.assertEqual(prepared.size, (320, 100))

    def test_prepare_image_respects_max_width(self):
        prepared = win_ocr._prepare_image(
            Image.new("RGB", (2000, 500), "white"), upscale=False, max_width=200
        )
        self.assertEqual(prepared.size, (200, 50))

    def test_prepare_image_downscales_oversized_frame(self):
        prepared = win_ocr._prepare_image(
            Image.new("RGB", (12000, 300), "white"), upscale=False
        )
        self.assertEqual(max(prepared.size), 10000)

    def test_pil_from_input_accepts_pil_bytes_and_path(self):
        img = make_image(["Hello."])
        self.assertEqual(win_ocr._pil_from_input(img).mode, "RGB")

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.assertEqual(win_ocr._pil_from_input(buf.getvalue()).size, img.size)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.png")
            img.save(path, format="PNG")
            self.assertEqual(win_ocr._pil_from_input(path).size, img.size)

    def test_none_input_returns_empty(self):
        self.assertEqual(WinOCR().read_text(None), "")
        self.assertEqual(WinOCR().read_items(None), [])

    def test_empty_result_does_not_fall_back_to_powershell(self):
        """Пустой кадр — валидный ответ winrt, а не повод запускать PowerShell."""
        ocr = WinOCR()
        calls = {"winrt": 0, "ps": 0}

        def winrt_rows(_img):
            calls["winrt"] += 1
            return []

        def ps_rows(_img):  # pragma: no cover - не должен вызваться
            calls["ps"] += 1
            return [([[0, 0]], "не должно случиться")]

        ocr._rows_with_winrt = winrt_rows
        ocr._rows_with_powershell = ps_rows

        self.assertEqual(ocr.read_text(make_image(["anything"])), "")
        self.assertEqual(calls, {"winrt": 1, "ps": 0})
        self.assertEqual(ocr.backend_name, "winrt")

    def test_backend_falls_back_and_cools_down_after_repeated_failures(self):
        ocr = WinOCR()
        calls = {"winrt": 0, "ps": 0}

        def winrt_rows(_img):
            calls["winrt"] += 1
            raise RuntimeError("simulated winrt failure")

        def ps_rows(_img):
            calls["ps"] += 1
            return [([[0, 0], [10, 0], [10, 10], [0, 10]], "из powershell")]

        ocr._rows_with_winrt = winrt_rows
        ocr._rows_with_powershell = ps_rows

        # Первые _FAILURE_THRESHOLD кадров пробуют winrt, потом он уходит в отпуск.
        for _ in range(win_ocr._FAILURE_THRESHOLD):
            self.assertEqual(ocr.read_text(make_image(["line"])), "из powershell")

        self.assertEqual(calls["winrt"], win_ocr._FAILURE_THRESHOLD)
        ocr.read_text(make_image(["line"]))
        self.assertEqual(calls["winrt"], win_ocr._FAILURE_THRESHOLD)
        self.assertEqual(ocr.backend_name, "powershell")
        self.assertIsInstance(ocr.last_error, RuntimeError)

    def test_success_resets_failure_counter(self):
        ocr = WinOCR()
        calls = {"winrt": 0}

        def flaky(_img):
            calls["winrt"] += 1
            if calls["winrt"] == 1:
                raise RuntimeError("one bad frame")
            return [([[0, 0]], "ок")]

        ocr._rows_with_winrt = flaky
        ocr._rows_with_powershell = lambda _img: [([[0, 0]], "из powershell")]

        self.assertEqual(ocr.read_text(make_image(["line"])), "из powershell")
        self.assertEqual(ocr.read_text(make_image(["line"])), "ок")
        self.assertEqual(ocr._failures[win_ocr.BACKEND_WINRT], 0)
        self.assertIsNone(ocr.last_error)

    def test_explicit_backend_does_not_try_others(self):
        ocr = WinOCR(backend="powershell")
        ocr._rows_with_winrt = lambda _img: [([[0, 0]], "winrt")]  # не должен вызваться
        ocr._rows_with_powershell = lambda _img: [([[0, 0]], "ps")]
        self.assertEqual(ocr.read_text(make_image(["line"])), "ps")

    def test_warmup_raises_when_all_backends_fail(self):
        ocr = WinOCR()

        def boom(_img):
            raise OcrUnavailableError("нет движка")

        ocr._rows_with_winrt = boom
        ocr._rows_with_powershell = boom
        with self.assertRaises(OcrUnavailableError):
            ocr.warmup()

    def test_powershell_recognize_requires_ok_sentinel(self):
        original = win_ocr._run_powershell
        try:
            win_ocr._run_powershell = lambda script, timeout=30: type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": "boom"}
            )()
            with self.assertRaises(OcrUnavailableError):
                win_ocr._powershell_recognize("x.png", "en-US")
        finally:
            win_ocr._run_powershell = original


@unittest.skipUnless(live_available(), "Windows OCR недоступен в этой системе")
class WinOCRLiveTests(unittest.TestCase):
    """Реальное распознавание через Windows.Media.Ocr."""

    def test_reads_synthetic_text(self):
        ocr = WinOCR()
        text = ocr.read_text(make_image(["Ryoshu: the city never sleeps."]))
        self.assertIn("Ryoshu", text)
        self.assertIn("city", text)

    def test_accepts_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.png")
            make_image(["Path based input."]).save(path, format="PNG")
            self.assertIn("Path", WinOCR().read_text(path))

    def test_easyocr_compatible_readtext(self):
        ocr = WinOCR()
        image = make_image(["Hello there.", "Second line."])

        plain = ocr.readtext(image, detail=0)
        self.assertIsInstance(plain, list)
        self.assertTrue(all(isinstance(item, str) for item in plain))

        detailed = ocr.readtext(image, detail=1)
        self.assertTrue(detailed)
        box, text, confidence = detailed[0]
        # Windows OCR иногда не отдаёт рамку строки — допускаем пустой box,
        # но если он есть, то это 4 точки, как у EasyOCR.
        self.assertIn(len(box), (0, 4))
        for point in box:
            self.assertEqual(len(point), 2)
        self.assertIsInstance(text, str)
        self.assertIsInstance(confidence, float)

    def test_extract_text_matches_read_text(self):
        ocr = WinOCR()
        image = make_image(["Alias works."])
        self.assertEqual(ocr.extract_text(image), ocr.read_text(image))

    def test_blank_image_returns_empty_string(self):
        ocr = WinOCR()
        self.assertEqual(ocr.read_text(Image.new("RGB", (320, 120), "white")).strip(), "")

    def test_warmup_reports_backend(self):
        self.assertIn(WinOCR().warmup(), ("winrt", "powershell"))


if __name__ == "__main__":
    unittest.main()
