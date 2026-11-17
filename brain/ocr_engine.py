# brain/ocr_engine.py
"""Обёртка над EasyOCR.

В v1.4.2 добавлен выбор устройства:
* "auto" (по умолчанию) — пробуем GPU, при ошибке тихо откатываемся на CPU;
* "gpu" — принудительно GPU (ошибка пробрасывается);
* "cpu" — принудительно CPU.

Раньше здесь стоял жёсткий gpu=False при комментарии про GPU, из-за чего
распознавание шло на процессоре и заметно тормозило.
"""
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)


class OCREngine:
    _reader_lock = threading.Lock()

    def __init__(self, device="auto"):
        import easyocr  # тяжёлый импорт: не тянем torch, пока движок не нужен

        device = (device or "auto").lower()
        if device not in ("auto", "gpu", "cpu"):
            device = "auto"

        self.reader = None
        if device in ("auto", "gpu"):
            try:
                log.info("Загрузка EasyOCR (GPU)...")
                self.reader = easyocr.Reader(["en"], gpu=True)
                log.info("EasyOCR запущен на GPU.")
            except Exception as exc:
                if device == "gpu":
                    raise
                log.warning("GPU недоступен для EasyOCR (%s) — переключаюсь на CPU.", exc)

        if self.reader is None:
            log.info("Загрузка EasyOCR (CPU)...")
            self.reader = easyocr.Reader(["en"], gpu=False)
            log.info("EasyOCR запущен на CPU.")

    def extract_text(self, img):
        """Принимает PIL Image, возвращает распознанный текст."""
        if img is None:
            return ""
        try:
            img_np = np.array(img.convert("RGB"))
        except Exception:
            return ""
        with self._reader_lock:
            bounds = self.reader.readtext(img_np, detail=0)
        return " ".join(bounds).strip()
