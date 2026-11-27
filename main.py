# main.py
"""Точка входа переводчика «Гаджет Будущего».

Запускается несколькими способами:
  python main.py                    — обычный запуск переводчика;
  python main.py --run-translator   — то же самое (флаг использует замороженный
                                      лаунчер, который перезапускает сам себя);
  python main.py --overlay-test     — показать тестовую карточку оверлея и выйти;
  python main.py --no-overlay       — консольный режим без оверлея (для отладки).

Модуль импортируется лаунчером gui_launcher.py: ``import main`` сам по себе
ничего не запускает. Лаунчер (в том числе собранный .exe) вызывает
``main.main(argv)``, передавая ему свои флаги командной строки.
"""

import argparse
import ctypes
import hashlib
import logging
import os
import sys
import threading
import time

from PIL import Image, ImageGrab

from brain.logging_setup import setup_logging
from brain.ocr_providers import (
    DEFAULT_PROVIDER,
    PROVIDER_EASYOCR,
    PROVIDER_LABELS,
    PROVIDER_WINDOWS,
    PROVIDERS,
)
from brain.overlay import Overlay
from brain.paths import data_location_hint, ensure_data_dir
from brain.providers import PROVIDERS, make_provider, settings_fingerprint
from brain.settings import load_settings
from brain.version import APP_TITLE, __version__

log = logging.getLogger(__name__)

# --- глобальное состояние (нужно для хоткеев и чистой остановки) ---
_MUTEX_NAME = "Local\\GadgetOfTheFuture_v142"
_ERROR_ALREADY_EXISTS = 183

_mutex_handle = None
_overlay = None
_stop_event = threading.Event()
_LAST_TRANSLATION = None
_pause_toggle = threading.Event()
_clear_request = threading.Event()
_last_error_card_at = 0.0

_ELEVATION_WARNING = (
    "Запущено с правами администратора: UIPI может скрыть оверлей — "
    "запусти без прав администратора"
)


def _is_windows():
    return os.name == "nt"


def _guard_std_streams():
    """В windowed-.exe sys.stdout и sys.stderr равны None.

    Сторонние библиотеки (EasyOCR, torch, tqdm) пишут в stdout/stderr напрямую
    и при None падают с AttributeError. Подменяем их на os.devnull: логи всё
    равно идут в data/gadget.log.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def _enable_dpi_awareness():
    """Делает окно оверлея DPI-осведомлённым.

    Без этого при масштабе Windows 125/150% система растягивает окно
    растром, и текст оверлея получается размытым. Вызывать нужно до создания
    первого окна Tk.
    """
    if not _is_windows():
        return
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _parse_log_level(name):
    try:
        return getattr(logging, str(name).upper())
    except (AttributeError, TypeError):
        return logging.INFO


# ============ выбор OCR-провайдера ============

def _create_ocr(settings):
    """Создаёт OCR-движок по настройке ``"ocr_provider"``.

    Импорты провайдеров ленивые: сборка v1.5.0 (Lightweight) не содержит
    easyocr/torch, поэтому brain.ocr_engine подгружается только по запросу.
    По умолчанию используется системный движок Windows (Windows Native).
    """
    provider = str(settings.get("ocr_provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        log.warning(
            "Неизвестный провайдер OCR %r — использую %s.", provider, DEFAULT_PROVIDER
        )
        provider = DEFAULT_PROVIDER

    if provider == PROVIDER_WINDOWS and not _is_windows():
        log.warning(
            "Windows Native OCR работает только в Windows — переключаюсь на EasyOCR."
        )
        return _finish_easyocr(settings)

    if provider == PROVIDER_WINDOWS:
        try:
            ocr = _create_windows_ocr()
        except Exception as exc:
            log.warning("Windows Native OCR недоступен: %s", exc)
            log.info("Пробую запасной движок EasyOCR...")
            try:
                return _finish_easyocr(settings)
            except Exception as fallback_exc:
                raise RuntimeError(
                    "Windows Native OCR недоступен (%s), а EasyOCR в этой сборке нет "
                    "(%s). Установите языковой пакет OCR: Параметры -> Время и язык -> "
                    "Язык -> Добавить язык (нужен английский), либо поставьте easyocr "
                    '(pip install easyocr) и задайте "ocr_provider": "easyocr".'
                    % (exc, fallback_exc)
                ) from exc

        log.info("Используется OCR: %s", PROVIDER_LABELS[PROVIDER_WINDOWS])
        log.info(
            "Windows OCR: язык %s, бэкенд %s",
            getattr(ocr, "language_tag", None) or "профиля Windows",
            getattr(ocr, "backend_name", None) or "?",
        )
        return ocr

    return _finish_easyocr(settings)


def _finish_easyocr(settings):
    """Создаёт EasyOCR и пишет в журнал, какой движок используется."""
    ocr = _create_easyocr(settings)
    log.info("Используется OCR: %s", PROVIDER_LABELS[PROVIDER_EASYOCR])
    return ocr


def _create_windows_ocr():
    """Windows Native OCR (Windows.Media.Ocr) — лёгкий, без torch."""
    try:
        from brain.ocr_providers.win_ocr import WinOCR
    except ImportError as exc:
        raise RuntimeError(
            "модуль Windows Native OCR не загрузился — похоже, не установлены "
            "пакеты winrt. Установите: pip install winrt-runtime "
            "winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
            "winrt-Windows.Storage.Streams winrt-Windows.Globalization"
        ) from exc

    ocr = WinOCR()
    backend = ocr.warmup()
    log.info("Windows Native OCR прогрелся (бэкенд %s).", backend)
    return ocr


def _create_easyocr(settings):
    """Старый движок EasyOCR; в Lightweight-сборке его нет по определению."""
    try:
        from brain.ocr_engine import OCREngine
    except ImportError as exc:
        raise RuntimeError(
            "модуль easyocr не импортируется (в сборке Lightweight нет easyocr и "
            "torch). Установите его командой pip install easyocr или переключитесь на "
            'Windows OCR: в data/settings.json задайте "ocr_provider": "windows"'
        ) from exc
    try:
        return OCREngine(settings.get("ocr_gpu", "auto"))
    except (ImportError, OSError) as exc:
        # OSError — это обычно не найденная DLL torch (msvcp140.dll и т.п.).
        raise RuntimeError("easyocr установлен, но не загружается: %s" % exc) from exc


def _acquire_single_instance():
    """Создаёт именованный мьютекс; False, если приложение уже запущено."""
    global _mutex_handle
    if not _is_windows():
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not _mutex_handle:
            return True
        return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _release_mutex():
    global _mutex_handle
    if _mutex_handle and _is_windows():
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            pass
    _mutex_handle = None


def _is_elevated():
    if not _is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ============ HotkeyManager ============

class HotkeyManager:
    """Горячие клавиши: RegisterHotKey (Win32) с фоллбэком на pynput."""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    _F_KEYS = {
        1: (0x75, "f6"),
        2: (0x76, "f7"),
        3: (0x77, "f8"),
        4: (0x78, "f9"),
    }

    def __init__(self, callbacks):
        self._callbacks = dict(callbacks)
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._registered = []
        self._backend = None
        self._pynput_listener = None

    def start(self):
        if _is_windows():
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._win32_loop, name="hotkeys-win32", daemon=True
            )
            self._thread.start()
            self._ready.wait(2.0)
            if self._backend == "win32":
                return "win32"
        if self._try_register_pynput():
            self._backend = "pynput"
            return "pynput"
        return None

    def stop(self):
        self._stop.set()
        if self._backend == "win32" and _is_windows():
            try:
                if self._thread_id:
                    ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=2.0)
        elif self._backend == "pynput":
            try:
                if self._pynput_listener is not None:
                    self._pynput_listener.stop()
            except Exception:
                pass
        self._backend = None

    def _win32_loop(self):
        try:
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._thread_id = kernel32.GetCurrentThreadId()

            user32.RegisterHotKey.restype = ctypes.c_bool
            user32.RegisterHotKey.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
            ]

            registered = []
            for hotkey_id, (vk, _name) in self._F_KEYS.items():
                if user32.RegisterHotKey(None, hotkey_id, 0, vk):
                    registered.append(hotkey_id)

            if len(registered) != len(self._F_KEYS):
                for hotkey_id in registered:
                    try:
                        user32.UnregisterHotKey(None, hotkey_id)
                    except Exception:
                        pass
                self._registered = []
                self._ready.set()
                return

            self._registered = registered
            self._backend = "win32"
            msg = wt.MSG()
            # Создаём очередь сообщений потока, чтобы WM_QUIT из stop() не потерялся.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0000)
            self._ready.set()

            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == self.WM_HOTKEY:
                    self._dispatch(msg.wParam)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("Ошибка в цикле горячих клавиш (win32)")
        finally:
            self._unregister_win32()
            self._backend = None
            self._ready.set()

    def _unregister_win32(self):
        if not _is_windows():
            return
        try:
            user32 = ctypes.windll.user32
            for hotkey_id in list(self._registered):
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
        except Exception:
            pass
        self._registered = []

    def _dispatch(self, hotkey_id):
        for key_id, (_vk, name) in self._F_KEYS.items():
            if key_id == hotkey_id:
                self._safe_call(name)
                return

    def _try_register_pynput(self):
        try:
            from pynput import keyboard
        except Exception:
            log.warning("pynput недоступен — горячие клавиши отключены.")
            return False
        try:
            self._pynput_listener = keyboard.GlobalHotKeys({
                "<f6>": lambda: self._safe_call("f6"),
                "<f7>": lambda: self._safe_call("f7"),
                "<f8>": lambda: self._safe_call("f8"),
                "<f9>": lambda: self._safe_call("f9"),
            })
            self._pynput_listener.daemon = True
            self._pynput_listener.start()
            return True
        except Exception:
            log.exception("Не удалось запустить pynput-бэкенд горячих клавиш")
            return False

    def _safe_call(self, name):
        callback = self._callbacks.get(name)
        if callback is None:
            return
        try:
            callback()
        except Exception:
            log.exception("Ошибка в обработчике горячей клавиши %s", name)


# ============ колбэки хоткеев ============

def _reshow_last_translation():
    if _overlay is not None and _LAST_TRANSLATION is not None:
        source, translation = _LAST_TRANSLATION
        _overlay.show_translation(source, translation)


def _toggle_overlay():
    if _overlay is not None:
        _overlay.toggle_visible()


_HOTKEY_CALLBACKS = {
    "f6": _reshow_last_translation,
    "f7": _toggle_overlay,
    "f8": _pause_toggle.set,
    "f9": _clear_request.set,
}


def _show_error_card_rate_limited(message=""):
    global _last_error_card_at
    now = time.time()
    if now - _last_error_card_at < 30.0:
        return
    _last_error_card_at = now
    if _overlay is not None:
        text = ("Ошибка: " + str(message)[:120]) if message else "Ошибка обработки кадра."
        _overlay.show_status(text, "error", duration=6)


# ============ цикл захвата ============

def _capture_loop(ocr, translate, analyze_character, clear_memory):
    global _LAST_TRANSLATION

    user32 = ctypes.windll.user32 if _is_windows() else None
    if user32 is not None:
        try:
            user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        except Exception:
            pass

    last_seq = None
    last_hash = None
    paused = False
    interval = 0.35

    while not _stop_event.is_set():
        try:
            if _pause_toggle.is_set():
                _pause_toggle.clear()
                paused = not paused
                if _overlay is not None:
                    if paused:
                        # Короткая карточка, а не «вечная» плашка: постоянный
                        # статус перекрывал лаунчер и другие окна.
                        _overlay.show_status("⏸ Пауза", "info", duration=4)
                    else:
                        _overlay.show_status("▶ Продолжаю", "ok", duration=3)
                log.info("Пауза: %s", "вкл" if paused else "выкл")

            if _clear_request.is_set():
                _clear_request.clear()
                clear_memory()
                if _overlay is not None:
                    _overlay.show_status("Память очищена", "ok", duration=4)
                log.info("Память диалога очищена.")

            if paused:
                time.sleep(interval)
                continue

            sequence = None
            if user32 is not None:
                try:
                    sequence = user32.GetClipboardSequenceNumber()
                except Exception:
                    sequence = None
            if sequence is not None and sequence == last_seq:
                time.sleep(interval)
                continue
            last_seq = sequence

            image = ImageGrab.grabclipboard()
            if isinstance(image, Image.Image):
                image_hash = hashlib.sha1(image.tobytes()).hexdigest()
                if image_hash == last_hash:
                    time.sleep(interval)
                    continue
                try:
                    text = ocr.extract_text(image)
                    if not text:
                        log.info("Текст на изображении не распознан.")
                    else:
                        if _overlay is not None:
                            _overlay.show_status("Нейросеть думает...", "info", duration=0)
                        log.info("OCR: %s", text)
                        translated = translate(text)
                        log.info("Перевод: %s", translated)
                        _LAST_TRANSLATION = (text, translated)
                        if _overlay is not None:
                            _overlay.show_translation(text, translated)
                        try:
                            analyze_character(text)
                        except Exception:
                            log.exception("Ошибка фонового анализа персонажа")
                    last_hash = image_hash
                except Exception as exc:
                    log.exception("Ошибка обработки кадра")
                    _show_error_card_rate_limited(exc)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.exception("Неожиданная ошибка в цикле захвата")
            _show_error_card_rate_limited(exc)

        time.sleep(interval)


# ============ режимы запуска ============

def _run_overlay_test(seconds):
    _guard_std_streams()
    _enable_dpi_awareness()
    setup_logging(logging.INFO)
    log.info("Тест оверлея v%s", __version__)
    # Настройки нужны, чтобы тестовая карточка красилась текущей темой, а не
    # всегдашним фиолетовым по умолчанию.
    try:
        settings = load_settings()
    except Exception:
        log.exception("Не удалось прочитать настройки — беру оформление по умолчанию")
        settings = None
    overlay = Overlay(settings)
    if not overlay.start():
        log.error("Не удалось запустить оверлей (нет GUI/Windows).")
        return 1
    overlay.show_translation(
        "Hello world! This is a sample English sentence to demonstrate the overlay card.",
        "Привет, мир! Это пример русского перевода для проверки оверлея: окно всегда "
        "поверх всех окон, не перехватывает фокус и пропускает клики мышью.",
    )
    try:
        time.sleep(max(0.0, float(seconds)))
    except KeyboardInterrupt:
        pass
    overlay.stop()
    return 0


def _create_llm_provider(settings):
    """Создаёт провайдера перевода по настройкам (None — если настроен неверно)."""
    name = str((settings or {}).get("provider") or "ollama").strip().lower()
    if name not in PROVIDERS:
        log.warning("Неизвестный провайдер «%s» — использую ollama.", name)
        name = "ollama"
    try:
        return make_provider(name, settings)
    except (PermissionError, ValueError) as exc:
        log.error("Перевод: не удалось создать провайдера «%s»: %s", name, exc)
        return None


def _translator_with_provider(translate, settings):
    """Оборачивает translate() провайдером, который переживает смену настроек.

    Провайдер создаётся один раз (он держит requests.Session с keep-alive), но
    если в лаунчере сменили провайдера/модель/ключ, он пересоздаётся на
    следующем кадре — прежнее поведение «настройки подхватываются без
    перезапуска переводчика» сохраняется.
    """
    state = {"settings": dict(settings or {}), "provider": _create_llm_provider(settings)}

    def _translate(text):
        try:
            fresh = load_settings()
        except Exception:
            fresh = state["settings"]
        if settings_fingerprint(fresh) != settings_fingerprint(state["settings"]):
            log.info(
                "Настройки перевода изменились — создаю нового провайдера (%s).",
                fresh.get("provider"),
            )
            previous = state["provider"]
            state["settings"] = fresh
            state["provider"] = _create_llm_provider(fresh)
            # Старый провайдер держит requests.Session: закрываем, чтобы после
            # нескольких переключений в лаунчере не копились соединения.
            if previous is not None and previous is not state["provider"]:
                try:
                    previous.close()
                except Exception:
                    pass

        provider = state["provider"]
        if provider is None:
            # translate() построит провайдера сама и вернёт понятное сообщение
            # об ошибке вместо падения цикла захвата.
            return translate(text)
        return translate(text, provider=provider)

    def _close():
        """Закрывает текущий провайдер (requests.Session) при остановке."""
        provider = state["provider"]
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass

    _translate.close = _close
    return _translate


def _migrate_character_cards():
    """Одноразовая миграция characters.json на карточки персонажей.

    Старое описание-строка становится полем description; перед перезаписью
    создаётся бэкап файла, поэтому данные пользователя не теряются.
    """
    from brain.memory_engine import migrate_characters_file

    try:
        migrated, backup = migrate_characters_file()
    except Exception:
        log.exception("Не удалось мигрировать characters.json на карточки")
        return

    if migrated:
        log.info(
            "[Лор] characters.json переведён на карточки: %d записей (бэкап: %s).",
            migrated,
            os.path.basename(backup) if backup else "не удалось создать",
        )


def _run_translator(args):
    global _overlay, _mutex_handle

    _guard_std_streams()
    _enable_dpi_awareness()
    _stop_event.clear()
    _pause_toggle.clear()
    _clear_request.clear()

    setup_logging(logging.INFO)
    log.info("Запуск переводчика v%s (%s)", __version__, APP_TITLE)
    log.info("Данные: %s", data_location_hint())
    ensure_data_dir()

    if not _acquire_single_instance():
        log.info("Приложение уже запущено — выходим.")
        if args.no_overlay:
            print("Приложение уже запущено.")
        return 0

    _migrate_character_cards()

    hotkeys = None
    close_translate = None
    try:
        from brain.settings import load_settings

        settings = load_settings()
        logging.getLogger().setLevel(_parse_log_level(settings.get("log_level")))
        elevated = _is_elevated()
        log.info("Права администратора: %s", elevated)
        if elevated:
            log.warning(_ELEVATION_WARNING)

        use_overlay = (not args.no_overlay) and bool(settings.get("overlay_enabled", True))
        _overlay = Overlay(settings) if use_overlay else None
        if _overlay is not None and not _overlay.start():
            log.warning("Оверлей не запустился — продолжаем без него.")
            _overlay = None

        if _overlay is not None:
            _overlay.show_status("Загрузка OCR...", "info", duration=0)

        started = time.time()
        ocr = _create_ocr(settings)
        log.info("OCR готов за %.2f с", time.time() - started)

        if _overlay is not None:
            if elevated:
                _overlay.show_status(_ELEVATION_WARNING, "error", duration=15)
            else:
                _overlay.show_status(
                    "Готово. F6 — повторить, F7 — скрыть, F8 — пауза, F9 — очистить память.",
                    "ok", duration=6,
                )

        from brain.llm_engine import analyze_character, translate
        from brain.memory_engine import clear_memory

        hotkeys = HotkeyManager(_HOTKEY_CALLBACKS)
        backend = hotkeys.start()
        log.info("Горячие клавиши: %s", backend or "недоступны")

        # Провайдер перевода создаётся здесь один раз (см. _create_llm_provider)
        # и передаётся в translate(); при смене настроек в лаунчере он
        # пересоздаётся обёрткой — перезапуск переводчика не нужен.
        _translate = _translator_with_provider(translate, settings)
        close_translate = getattr(_translate, "close", None)

        # Настройки передаём в анализ лора: он должен считать той же моделью,
        # что выбрана для перевода (Ollama или DeepSeek), а не отдельной.
        # Перечитываем их на каждого нового персонажа — как и для перевода,
        # поэтому смена провайдера подхватывается без перезапуска.
        def _analyze_character(text):
            try:
                current_settings = load_settings()
            except Exception:
                current_settings = settings
            return analyze_character(text, current_settings)

        _capture_loop(ocr, _translate, _analyze_character, clear_memory)
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C.")
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("Не удалось запустить переводчик")
        # Без этого пользователь видит только мигание оверлея и тихий выход.
        if _overlay is not None:
            try:
                _overlay.show_status("Ошибка запуска: " + str(exc)[:140], "error", duration=0)
                time.sleep(4.0)
            except Exception:
                pass
        return 1
    finally:
        if callable(close_translate):
            close_translate()
        if hotkeys is not None:
            hotkeys.stop()
        if _overlay is not None:
            _overlay.stop()
            _overlay = None
        _release_mutex()
        _mutex_handle = None

    return 0


# ============ точка входа ============

def _build_arg_parser():
    parser = argparse.ArgumentParser(prog="GadgetOfTheFuture", add_help=True)
    parser.add_argument(
        "--run-translator", action="store_true",
        help="Запустить переводчик (используется замороженным лаунчером).",
    )
    parser.add_argument(
        "--overlay-test", action="store_true",
        help="Показать тестовую карточку оверлея и выйти.",
    )
    parser.add_argument(
        "--seconds", type=float, default=4.0,
        help="Сколько секунд держать тестовую карточку (для --overlay-test).",
    )
    parser.add_argument(
        "--no-overlay", action="store_true",
        help="Консольный режим без оверлея (для отладки).",
    )
    return parser


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    if args.overlay_test:
        return _run_overlay_test(args.seconds)
    return _run_translator(args)


if __name__ == "__main__":
    sys.exit(main())
