# brain/ocr_providers/__init__.py
"""Провайдеры распознавания текста (OCR).

Сейчас их два:

* ``windows`` — системный движок Windows (``Windows.Media.Ocr``), см.
  :mod:`brain.ocr_providers.win_ocr`. Лёгкий: ни torch, ни моделей на диске.
* ``easyocr`` — прежний движок (:mod:`brain.ocr_engine`). Оставлен в исходниках
  и включается настройкой ``"ocr_provider": "easyocr"``, но в сборку v1.5.0
  (Lightweight) не входит, потому что тянет torch (~2 ГБ).

Импорт этого пакета намеренно не тянет ни winrt, ни torch: провайдер
выбирается лениво в :func:`main._create_ocr`.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

PROVIDER_WINDOWS = "windows"
PROVIDER_EASYOCR = "easyocr"
DEFAULT_PROVIDER = PROVIDER_WINDOWS
PROVIDERS = (PROVIDER_WINDOWS, PROVIDER_EASYOCR)

# Человекочитаемые подписи — используются в логах и в карточке настроек.
PROVIDER_LABELS = {
    PROVIDER_WINDOWS: "Windows Native",
    PROVIDER_EASYOCR: "EasyOCR",
}

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_EASYOCR",
    "PROVIDER_LABELS",
    "PROVIDER_WINDOWS",
    "PROVIDERS",
    "WinOCR",
]


def __getattr__(name):
    # Ленивый доступ: ``from brain.ocr_providers import WinOCR`` не импортирует
    # winrt до первого обращения (важно для сборки без winrt).
    if name == "WinOCR":
        try:
            from .win_ocr import WinOCR
        except ImportError as exc:
            # hasattr()/getattr(default) должны видеть AttributeError, а не
            # ImportError — иначе ломается проверка возможностей.
            raise AttributeError(f"WinOCR недоступен: {exc}") from exc

        return WinOCR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
