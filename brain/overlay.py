# brain/overlay.py
"""Оверлей перевода «Гаджет Будущего» поверх игры (always-on-top, click-through).

Почему оверлей нужен и почему раньше его не было видно:
``build.bat`` собирал exe с флагом PyInstaller ``--uac-admin``, поэтому приложение
запускалось с повышенными правами (high integrity). Windows UIPI (User Interface
Privilege Isolation) запрещает high-integrity процессу рисовать свои окна ПОВЕРХ
окон low/medium integrity (игры, браузеры, блокнот). Окно оверлея создавалось и
текст переводился, но оно физически не могло оказаться выше обычных окон.

Решение архитектурное: переводчик должен работать с обычными правами (без
администратора). Захват скриншота через буфер обмена, глобальные хоткеи, EasyOCR
и HTTP-перевод нормально работают без прав. Оверлею дополнительно нужны явные
расширенные стили Win32 (layered, transparent, toolwindow, noactivate, topmost),
чтобы он был «прозрачным» для кликов, не забирал фокус и был поверх всего.

Все операции с Tk выполняются в отдельном daemon-потоке; вызывающий код общается
с оверлеем только через потокобезопасную очередь. Если Tk недоступен или не
может стартовать, класс тихо отключается, и приложение продолжает работать.
"""

import ctypes
import logging
import os
import queue
import threading

from brain.version import __version__

log = logging.getLogger(__name__)

try:
    import tkinter as tk
except Exception:  # tkinter может отсутствовать (headless-сервер и т.п.)
    tk = None

# --- палитра карточки ---
TRANSPARENT_KEY = "#010203"  # цвет, который никогда не встречается в карточке
CARD = "#12121A"
CARD_BORDER = "#2A2A3A"
TEXT = "#E6E6EF"
MUTED = "#8A8AA0"
BG = None

ACCENTS = {
    "violet": "#7C3AED",
    "blue": "#2563EB",
    "green": "#16A34A",
    "orange": "#EA580C",
    "crimson": "#DC2626",
    "mono": "#6B7280",
}
STATUS_ERROR = "#F87171"

_SHADOW_OFFSET = 3
_CARD_RADIUS = 18
_PAD_X = 18
_PAD_Y = 16

# --- Win32-константы (применяются только на Windows) ---
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOPMOST = 0x00000008
_EX_STYLE_MASK = (
    _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOOLWINDOW
    | _WS_EX_NOACTIVATE | _WS_EX_TOPMOST
)

# ВАЖНО (исправление v1.4.2+): WS_EX_LAYERED нельзя вешать на дочернее окно Tk.
# DWM композитит layered-child без собственных layered-атрибутов сплошным
# чёрным прямоугольником — именно из-за этого оверлей был «чёрным». Слой
# (layering) задаёт только обёртка TkTopLevel; дочернему окну нужны лишь
# click-through/toolwindow/noactivate.
_CLIENT_EX_STYLE_MASK = _EX_STYLE_MASK & ~_WS_EX_LAYERED
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

_DEFAULTS = {
    "overlay_position": "bottom-center",
    "overlay_opacity": 0.93,
    "overlay_font_size": 18,
    "overlay_width": 640,
    "overlay_duration": 12.0,
    "overlay_show_source": True,
    "overlay_accent": "violet",
}

_POSITIONS = (
    "bottom-center", "top-center", "center",
    "top-left", "top-right", "bottom-left", "bottom-right",
)


def _is_windows():
    return os.name == "nt"


def _clamp_float(value, low, high, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clamp_int(value, low, high, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _user32():
    if not _is_windows():
        return None
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def _set_window_long(hwnd, index, value):
    user32 = _user32()
    if user32 is None:
        return 0
    fn = getattr(user32, "SetWindowLongPtrW", None) or getattr(user32, "SetWindowLongW", None)
    if fn is None:
        return 0
    fn.restype = ctypes.c_ssize_t
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    return fn(ctypes.c_void_p(hwnd), index, value)


def _get_window_long(hwnd, index):
    user32 = _user32()
    if user32 is None:
        return 0
    fn = getattr(user32, "GetWindowLongPtrW", None) or getattr(user32, "GetWindowLongW", None)
    if fn is None:
        return 0
    fn.restype = ctypes.c_ssize_t
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return fn(ctypes.c_void_p(hwnd), index)


def _set_window_pos_topmost(hwnd):
    user32 = _user32()
    if user32 is None:
        return
    try:
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(_HWND_TOPMOST), 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )
    except Exception:
        pass


# ============ класс Overlay ============

class Overlay:
    """Потокобезопасный оверлей перевода на tkinter (полноценен только на Windows)."""

    def __init__(self, settings=None):
        self._settings = dict(_DEFAULTS)
        if settings:
            for key, value in settings.items():
                if value is not None:
                    self._settings[key] = value

        self._queue = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._running = False

        # Всё, что ниже, принадлежит только Tk-потоку.
        self.root = None
        self.canvas = None
        self._hwnd = 0
        self._parent_hwnd = 0
        self._visible = False
        self._mode = None
        self._translation = ""
        self._source = ""
        self._status_text = ""
        self._status_kind = "info"
        self._auto_hide_ms = 0
        self._hide_after_id = None
        self._fade_after_id = None
        self._card_width = 640
        self._card_height = 80
        self._opacity = 0.93
        self._font_size = 18
        self._width = 640
        self._duration = 12.0
        self._show_source = True
        self._accent = "violet"
        self._position = "bottom-center"

    def start(self, timeout=8.0):
        if self._running and self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._stop_requested.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, name="overlay-tk", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return self._running and self._ready.is_set()

    def show_translation(self, source, translation):
        if not self._running:
            return
        self._queue.put(("show_translation", source, translation))

    def show_status(self, text, kind="info", duration=None):
        if not self._running:
            return
        self._queue.put(("show_status", text, kind, duration))

    def update_config(self, settings):
        if not settings:
            return
        if not self._running:
            for key, value in settings.items():
                if value is not None:
                    self._settings[key] = value
            return
        self._queue.put(("update_config", dict(settings)))

    def hide(self):
        if not self._running:
            return
        self._queue.put(("hide",))

    def toggle_visible(self):
        if not self._running:
            return
        self._queue.put(("toggle_visible",))

    def stop(self):
        if not self._running:
            self._running = False
            return
        self._stop_requested.set()
        try:
            self._queue.put(("stop",))
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._running = False

    def debug_hwnd_info(self):
        result = {
            "running": self._running,
            "visible": False,
            "hwnd": 0,
            "parent": 0,
            "ex_style": 0,
            "layered": False,
            "click_through": False,
            "toolwindow": False,
            "noactivate": False,
            "topmost": False,
            "geometry": "",
            "opacity": self._settings.get("overlay_opacity", 0.93),
            "position": self._settings.get("overlay_position", "bottom-center"),
            "last_text": "",
        }
        if not self._running:
            return result
        event = threading.Event()
        self._queue.put(("debug", result, event))
        event.wait(2.0)
        return result

    @property
    def is_running(self):
        return self._running


# ============ Tk-поток ============

    def _thread_main(self):
        if tk is None:
            log.error("tkinter недоступен — оверлей отключён.")
            self._running = False
            self._ready.set()
            return
        try:
            root = tk.Tk()
        except Exception as exc:
            log.error("Не удалось создать окно оверлея (нет GUI/дисплея): %s", exc)
            self._running = False
            self._ready.set()
            return

        self.root = root
        try:
            self._read_settings()
            self._build_window()
            self._ready.set()
            root.after(30, self._poll)
            root.mainloop()
        except Exception:
            log.exception("Критическая ошибка в потоке оверлея")
        finally:
            self._running = False
            try:
                self._cancel_hide()
                self._cancel_fade()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
            # Убираем все ссылки на Tk-объекты именно в этом потоке, чтобы
            # Tcl-интерпретатор удалился здесь же, а не в главном потоке при
            # завершении процесса. Иначе Tcl аварийно завершает процесс:
            # «Tcl_AsyncDelete: async handler deleted by the wrong thread».
            try:
                tk._default_root = None
            except Exception:
                pass
            self.root = None
            self.canvas = None

    def _read_settings(self):
        settings = self._settings
        position = str(settings.get("overlay_position") or "bottom-center")
        self._position = position if position in _POSITIONS else "bottom-center"
        self._opacity = _clamp_float(settings.get("overlay_opacity"), 0.3, 1.0, 0.93)
        self._font_size = _clamp_int(settings.get("overlay_font_size"), 11, 40, 18)
        self._width = _clamp_int(settings.get("overlay_width"), 320, 1400, 640)
        self._duration = _clamp_float(settings.get("overlay_duration"), 2.0, 120.0, 12.0)
        self._show_source = bool(settings.get("overlay_show_source", True))
        accent = str(settings.get("overlay_accent") or "violet")
        self._accent = accent if accent in ACCENTS else "violet"

    def _build_window(self):
        root = self.root
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.attributes("-alpha", self._opacity)
        except Exception:
            pass
        root.configure(bg=TRANSPARENT_KEY)
        try:
            root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            pass

        self.canvas = tk.Canvas(root, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=False)

        root.update_idletasks()
        self._apply_win32_styles()
        root.withdraw()

    def _apply_win32_styles(self):
        if not _is_windows():
            return
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
        except Exception as exc:
            log.warning("Не удалось получить HWND оверлея: %s", exc)
            return
        try:
            user32 = _user32()
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            parent = user32.GetParent(hwnd) or 0

            # Обёртка — настоящий layered-верхнеуровневый окно: полная маска.
            if parent:
                current = _get_window_long(parent, _GWL_EXSTYLE)
                _set_window_long(parent, _GWL_EXSTYLE, current | _EX_STYLE_MASK)
                _set_window_pos_topmost(parent)

            # Дочернее окно: всё, кроме WS_EX_LAYERED (см. комментарий выше).
            current = _get_window_long(hwnd, _GWL_EXSTYLE)
            _set_window_long(
                hwnd, _GWL_EXSTYLE,
                (current | _CLIENT_EX_STYLE_MASK) & ~_WS_EX_LAYERED,
            )
            _set_window_pos_topmost(hwnd)

            self._hwnd = hwnd
            self._parent_hwnd = parent or 0
        except Exception as exc:
            log.warning("Не удалось применить Win32-стили оверлея: %s", exc)

    def _ensure_styles(self):
        self._apply_win32_styles()

    def _poll(self):
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "stop":
                    self._stop_requested.set()
                    try:
                        self._cancel_hide()
                        self._cancel_fade()
                    except Exception:
                        pass
                    self.root.quit()
                    return
                try:
                    self._handle(item)
                except Exception:
                    log.exception("Ошибка обработки команды оверлея")
        except queue.Empty:
            pass
        if not self._stop_requested.is_set():
            self.root.after(30, self._poll)

    def _handle(self, item):
        command = item[0]
        if command == "show_translation":
            self._handle_show_translation(item[1], item[2])
        elif command == "show_status":
            self._handle_show_status(item[1], item[2], item[3])
        elif command == "update_config":
            self._handle_update_config(item[1])
        elif command == "hide":
            self._hide_now()
        elif command == "toggle_visible":
            self._handle_toggle()
        elif command == "debug":
            self._handle_debug(item[1], item[2])


# ============ обработчики ============

    def _handle_show_translation(self, source, translation):
        self._mode = "translation"
        self._translation = translation or ""
        self._source = source or ""
        self._status_text = ""
        self._status_kind = "info"
        self._auto_hide_ms = int(self._duration * 1000)
        self._show()

    def _handle_show_status(self, text, kind, duration):
        self._mode = "status"
        self._status_text = text or ""
        self._status_kind = kind if kind in ("info", "ok", "error") else "info"
        self._translation = ""
        self._source = ""
        if duration is None:
            self._auto_hide_ms = int(self._duration * 1000)
        elif duration > 0:
            self._auto_hide_ms = int(duration * 1000)
        else:
            self._auto_hide_ms = 0
        self._show()

    def _handle_update_config(self, settings):
        if not settings:
            return
        self._cancel_fade()
        for key in (
            "overlay_position", "overlay_opacity", "overlay_font_size",
            "overlay_width", "overlay_duration", "overlay_show_source", "overlay_accent",
        ):
            if key in settings and settings[key] is not None:
                self._settings[key] = settings[key]
        self._read_settings()
        if self._visible:
            try:
                self.root.attributes("-alpha", self._opacity)
            except Exception:
                pass
        self._draw()
        if self._visible:
            self._ensure_styles()

    def _handle_toggle(self):
        if self._visible:
            self._hide_now()
        elif self._mode in ("translation", "status"):
            self._show()

    def _handle_debug(self, result, event):
        result["running"] = self._running
        result["visible"] = self._visible
        result["position"] = self._position
        result["opacity"] = self._opacity
        result["last_text"] = (
            self._translation if self._mode == "translation" else self._status_text
        )
        result["geometry"] = ""
        result["hwnd"] = 0
        result["parent"] = self._parent_hwnd or 0
        result["ex_style"] = 0
        combined = 0
        try:
            result["geometry"] = self.root.geometry()
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            result["hwnd"] = hwnd
            parent = self._parent_hwnd or 0
            result["parent"] = parent
            client_style = _get_window_long(hwnd, _GWL_EXSTYLE) if hwnd else 0
            parent_style = _get_window_long(parent, _GWL_EXSTYLE) if parent else 0
            combined = client_style | parent_style
            result["ex_style"] = client_style & 0xFFFFFFFF
        except Exception:
            pass
        result["layered"] = bool(combined & _WS_EX_LAYERED)
        result["click_through"] = bool(combined & _WS_EX_TRANSPARENT)
        result["toolwindow"] = bool(combined & _WS_EX_TOOLWINDOW)
        result["noactivate"] = bool(combined & _WS_EX_NOACTIVATE)
        result["topmost"] = bool(combined & _WS_EX_TOPMOST)
        event.set()


# ============ показ/скрытие ============

    def _show(self):
        self._cancel_hide()
        self._cancel_fade()
        self._draw()
        try:
            self.root.deiconify()
        except Exception:
            pass
        self.root.update_idletasks()
        self._ensure_styles()
        self._visible = True
        self._fade_in()
        if self._auto_hide_ms > 0:
            self._hide_after_id = self.root.after(int(self._auto_hide_ms), self._on_auto_hide)

    def _on_auto_hide(self):
        self._hide_after_id = None
        self._fade_out(self._withdraw)

    def _withdraw(self):
        self._visible = False
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _hide_now(self):
        self._cancel_hide()
        self._cancel_fade()
        self._visible = False
        if self.root is not None:
            try:
                self.root.withdraw()
            except Exception:
                pass

    def _cancel_hide(self):
        if self._hide_after_id is not None and self.root is not None:
            try:
                self.root.after_cancel(self._hide_after_id)
            except Exception:
                pass
        self._hide_after_id = None

    def _cancel_fade(self):
        if self._fade_after_id is not None and self.root is not None:
            try:
                self.root.after_cancel(self._fade_after_id)
            except Exception:
                pass
        self._fade_after_id = None

    def _fade_in(self):
        self._cancel_fade()
        target = self._opacity
        start = max(0.0, target * 0.55)
        try:
            self.root.attributes("-alpha", start)
        except Exception:
            return
        self._fade_step(start, target, 6, None)

    def _fade_out(self, then=None):
        self._cancel_fade()
        try:
            current = float(self.root.attributes("-alpha"))
        except Exception:
            current = 0.0
        if current <= 0.0:
            if then:
                then()
            return
        self._fade_step(current, 0.0, 12, then)

    def _fade_step(self, current, target, remaining, then):
        if remaining <= 0 or self._stop_requested.is_set():
            try:
                self.root.attributes("-alpha", target)
            except Exception:
                pass
            if then:
                then()
            return
        delta = (target - current) / remaining
        nxt = current + delta
        done = False
        if (target >= current and nxt >= target) or (target <= current and nxt <= target):
            nxt = target
            done = True
        try:
            self.root.attributes("-alpha", nxt)
        except Exception:
            done = True
        if done:
            if then:
                then()
            return
        self._fade_after_id = self.root.after(20, self._fade_step, nxt, target, remaining - 1, then)


# ============ отрисовка ============

    def _accent_color(self):
        # Статусы «ok»/«info» красятся темой, как и карточка перевода: раньше
        # «Готово. F6…» и «Продолжаю» были зелёными независимо от темы.
        if self._mode == "status" and self._status_kind == "error":
            return STATUS_ERROR
        return ACCENTS.get(self._accent, ACCENTS["violet"])

    def _truncate_source(self, text, wrap):
        if not text:
            return ""
        font_size = max(11, self._font_size - 5)
        chars_per_line = max(20, int(wrap / (font_size * 0.55)))
        limit = chars_per_line * 2
        if len(text) > limit:
            return text[: max(1, limit - 1)].rstrip() + "…"
        return text

    def _draw(self):
        if self.canvas is None:
            return
        canvas = self.canvas
        canvas.delete("all")

        screen_w = self.root.winfo_screenwidth()
        self._card_width = max(160, min(self._width, screen_w - 64))
        width = self._card_width
        wrap = width - 2 * _PAD_X
        accent = self._accent_color()

        # Индикатор-кружочек убран: он всегда был цвета акцента и ничего не
        # сообщал о состоянии (состояние и так видно по тексту карточки).
        header = "ГАДЖЕТ БУДУЩЕГО · v" + __version__
        canvas.create_text(
            _PAD_X, _PAD_Y + 4, text=header, anchor="nw",
            font=("Segoe UI", 10, "bold"), fill=MUTED, tags="text",
        )
        content_top = _PAD_Y + 18

        if self._mode == "status":
            canvas.create_text(
                _PAD_X, content_top, text=self._status_text or "",
                anchor="nw", font=("Segoe UI", self._font_size), fill=TEXT,
                width=wrap, justify="left", tags="text",
            )
        else:
            main_item = canvas.create_text(
                _PAD_X, content_top, text=self._translation or "",
                anchor="nw", font=("Segoe UI", self._font_size), fill=TEXT,
                width=wrap, justify="left", tags="text",
            )
            self.root.update_idletasks()
            bbox = canvas.bbox(main_item)
            main_bottom = bbox[3] if bbox else content_top
            if self._show_source and self._source:
                source_font_size = max(11, self._font_size - 5)
                source_text = "EN: " + self._truncate_source(self._source, wrap)
                canvas.create_text(
                    _PAD_X, main_bottom + 8, text=source_text, anchor="nw",
                    font=("Segoe UI", source_font_size), fill=MUTED,
                    width=wrap, justify="left", tags="text",
                )

        self.root.update_idletasks()
        bbox = canvas.bbox("text")
        content_bottom = bbox[3] if bbox else content_top
        card_height = content_bottom + _PAD_Y

        self._rounded_poly(
            canvas, _SHADOW_OFFSET, _SHADOW_OFFSET, width + _SHADOW_OFFSET,
            card_height + _SHADOW_OFFSET, _CARD_RADIUS,
            fill="#000000", outline="", tags="shadow",
        )
        self._rounded_poly(
            canvas, 0, 0, width, card_height, _CARD_RADIUS,
            fill=CARD, outline=accent, width=2, tags="card",
        )
        canvas.tag_lower("card")
        canvas.tag_lower("shadow")

        canvas.config(width=width + _SHADOW_OFFSET, height=card_height + _SHADOW_OFFSET)
        self._card_height = card_height
        self._apply_geometry()

    @staticmethod
    def _rounded_poly(canvas, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2 - radius,
            x1, y1 + radius,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=28, **kwargs)

    def _apply_geometry(self):
        if self.root is None:
            return
        width = self._card_width + _SHADOW_OFFSET
        height = self._card_height + _SHADOW_OFFSET
        x, y = self._compute_position(width, height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _compute_position(self, width, height):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        margin = 32
        position = self._position
        if position == "top-center":
            x, y = (screen_w - width) // 2, margin
        elif position == "center":
            x, y = (screen_w - width) // 2, (screen_h - height) // 2
        elif position == "top-left":
            x, y = margin, margin
        elif position == "top-right":
            x, y = screen_w - width - margin, margin
        elif position == "bottom-left":
            x, y = margin, screen_h - height - margin
        elif position == "bottom-right":
            x, y = screen_w - width - margin, screen_h - height - margin
        else:
            x, y = (screen_w - width) // 2, screen_h - height - margin
        x = max(0, min(int(x), max(0, screen_w - width)))
        y = max(0, min(int(y), max(0, screen_h - height)))
        return int(x), int(y)


if __name__ == "__main__":
    import json
    import sys
    import time

    overlay = Overlay()
    if not overlay.start():
        print("Оверлей не запустился (нет GUI/Windows).")
        sys.exit(1)
    overlay.show_translation(
        "Hello world! This is a long sample English sentence used to demonstrate the "
        "translation overlay card with an always-on-top, click-through window.",
        "Привет, мир! Это длинный пример русского перевода для демонстрации карточки "
        "оверлея: окно всегда поверх всех окон, не перехватывает фокус и пропускает "
        "клики мышью, а скруглённые углы карточки по-настоящему прозрачны.",
    )
    time.sleep(1.0)
    print(json.dumps(overlay.debug_hwnd_info(), ensure_ascii=False))
    time.sleep(5.0)
    overlay.stop()
    sys.exit(0)
