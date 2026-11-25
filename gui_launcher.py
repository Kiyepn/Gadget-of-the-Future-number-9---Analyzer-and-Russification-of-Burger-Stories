# gui_launcher.py
"""Графический лаунчер «Гаджет Будущего» v1.5.1.

Что изменилось в v1.5.1 (багфиксы после релиза 1.5.0):

* **Проверка Ollama.** При старте и при выборе «Ollama (локально)» лаунчер
  проверяет CLI (`shutil.which`) и сервис (`http://127.0.0.1:11434`). Если
  Ollama нет — вместо строки в журнале показывается баннер с кнопкой «Скачать
  Ollama», которая открывает https://ollama.com/download/windows.
* **Загрузка модели с прогрессом.** Вместо скрытого `ollama pull` — потоковая
  загрузка через `POST /api/pull` (NDJSON): полоска прогресса, проценты, объём
  в ГБ и консоль со стадиями («pulling manifest», «verifying sha256 digest»…).
  Есть отмена. Если API недоступен, используется CLI с определением прогресса
  по `ollama list`.
* **Ярлык на рабочем столе** создаётся при первом запуске собранного .exe
  (настройка «Создавать ярлык на рабочем столе» + кнопка «Создать ярлык»).
* **Оформление лаунчера**: темы (фиолетовая/синяя/зелёная/оранжевая/багровая/
  моно) применяются ко всему окну, а не только к карточке перевода.

Что было раньше (v1.4.2):

* Переводчик больше не печатает перевод в консоль: он показывает его в
  оверлее поверх игры (brain/overlay.py), а лаунчер показывает живой лог.
* Лаунчер не запрашивает права администратора: иначе UIPI делал оверлей
  невидимым поверх обычных окон.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque

import requests

# Гарантируем, что корень проекта в sys.path (нужно для импорта пакета brain).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Собранный .exe — это лаунчер. Чтобы флаги переводчика работали и в нём,
# передаём их main.py: --run-translator (запуск переводчика), --overlay-test
# (проверка оверлея) и --no-overlay.
_DELEGATED_FLAGS = ("--run-translator", "--overlay-test", "--no-overlay")
if any(flag in sys.argv for flag in _DELEGATED_FLAGS):
    import main  # noqa: E402

    sys.exit(main.main(sys.argv[1:]))

try:
    import customtkinter as ctk
except ImportError:
    sys.stderr.write(
        "Не установлен CustomTkinter.\n"
        "Установите его командой:  pip install customtkinter\n"
    )
    sys.exit(1)

from brain.logging_setup import log_file_path, setup_logging
from brain.paths import app_root, data_location_hint
from brain.settings import LAUNCHER_THEMES, load_settings, save_settings
from brain.version import __version__

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# --- Ollama ---
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/windows"
OLLAMA_PING_TIMEOUT = 1.5
OLLAMA_MODEL_TIMEOUT = 2.5

# --- Палитры лаунчера -------------------------------------------------------
# Тема меняет оформление самого лаунчера (фон, карточки, акцент), а не только
# карточки перевода. Значения подставляются в модульные глобалы ниже, поэтому
# после смены темы интерфейс пересобирается (см. _rebuild_ui).
THEMES = {
    "violet": {
        "bg": "#12121a", "card": "#1c1c28", "border": "#2a2a3a",
        "text": "#e6e6ef", "muted": "#8a8aa0",
        "accent": "#7c3aed", "accent_hover": "#8f52ff",
    },
    "blue": {
        "bg": "#0e1621", "card": "#16222f", "border": "#233648",
        "text": "#e4eef7", "muted": "#8aa0b4",
        "accent": "#2563eb", "accent_hover": "#3b82f6",
    },
    "green": {
        "bg": "#0f1714", "card": "#17251f", "border": "#264034",
        "text": "#e4f2ea", "muted": "#8cb09c",
        "accent": "#16a34a", "accent_hover": "#22c55e",
    },
    "orange": {
        "bg": "#1a1410", "card": "#251b14", "border": "#3d2b1e",
        "text": "#f5e9dd", "muted": "#b39a83",
        "accent": "#ea580c", "accent_hover": "#f97316",
    },
    "crimson": {
        "bg": "#1a1013", "card": "#251519", "border": "#3d2229",
        "text": "#f7e4e8", "muted": "#b4878f",
        "accent": "#dc2626", "accent_hover": "#ef4444",
    },
    "mono": {
        "bg": "#101012", "card": "#1a1a1d", "border": "#2c2c31",
        "text": "#e8e8ea", "muted": "#9a9aa2",
        "accent": "#6b7280", "accent_hover": "#9ca3af",
    },
}

THEME_LABELS = {
    "Фиолетовая": "violet",
    "Синяя": "blue",
    "Зелёная": "green",
    "Оранжевая": "orange",
    "Багровая": "crimson",
    "Монохромная": "mono",
}
THEME_KEYS = {v: k for k, v in THEME_LABELS.items()}

# Значения по умолчанию; apply_theme() перезаписывает их при старте и при
# смене темы, а интерфейс строится уже с новыми цветами.
BG = THEMES["violet"]["bg"]
CARD = THEMES["violet"]["card"]
CARD_BORDER = THEMES["violet"]["border"]
TEXT = THEMES["violet"]["text"]
MUTED = THEMES["violet"]["muted"]
ACCENT = THEMES["violet"]["accent"]
ACCENT_HOVER = THEMES["violet"]["accent_hover"]

SUCCESS = "#4ade80"
ERROR = "#f87171"
WARN = "#fbbf24"


def apply_theme(name):
    """Применяет палитру лаунчера. Возвращает фактическое имя темы."""
    global BG, CARD, CARD_BORDER, TEXT, MUTED, ACCENT, ACCENT_HOVER
    name = (name or "violet").lower()
    if name not in THEMES:
        name = "violet"
    theme = THEMES[name]
    BG = theme["bg"]
    CARD = theme["card"]
    CARD_BORDER = theme["border"]
    TEXT = theme["text"]
    MUTED = theme["muted"]
    ACCENT = theme["accent"]
    ACCENT_HOVER = theme["accent_hover"]
    return name


def normalize_model_name(name):
    """«translategemma:4b» -> («translategemma», «4b»); без тега — «latest»."""
    raw = (name or "").strip().lower()
    if not raw:
        return "", ""
    if ":" in raw:
        base, tag = raw.rsplit(":", 1)
        return base, (tag or "latest")
    return raw, "latest"


def model_names_match(left, right):
    """Сравнивает имена моделей Ollama с учётом неявного тега «latest»."""
    return normalize_model_name(left) == normalize_model_name(right)


def format_bytes(value):
    """Человекочитаемый объём для прогресса загрузки."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} ГБ"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} МБ"
    if value >= 1024:
        return f"{value / 1024:.0f} КБ"
    return f"{int(value)} Б"


def format_speed(value):
    """Скорость загрузки: «12.3 МБ/с», «800 КБ/с»."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    if value <= 0:
        return "0 КБ/с"
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} ГБ/с"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} МБ/с"
    return f"{value / 1024:.0f} КБ/с"


def format_duration(seconds):
    """Сколько осталось: «~15 с», «~4 мин», «~1 ч 5 мин»."""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if seconds < 1:
        return "меньше секунды"
    if seconds < 60:
        return f"~{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"~{minutes} мин {sec:02d} с" if sec else f"~{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours} ч {minutes:02d} мин"


class DownloadTracker:
    """Общий прогресс, скорость и ETA загрузки модели по событиям /api/pull.

    Ollama присылает прогресс по слоям: у каждого свой ``digest``, ``completed``
    и ``total``. Поэтому «общий процент» считается как сумма по всем слоям, а
    скорость — по скользящему окну последних секунд (мгновенная скорость скачет
    на старте каждого слоя).
    """

    WINDOW_SECONDS = 8.0

    def __init__(self):
        self.layers = {}        # digest -> [completed, total]
        self.samples = deque()  # (monotonic_ts, completed_bytes)
        self.started_at = None

    def update(self, digest=None, completed=None, total=None, now=None):
        now = time.monotonic() if now is None else float(now)
        if self.started_at is None:
            self.started_at = now

        if digest:
            entry = self.layers.setdefault(str(digest), [0, 0])
            if total:
                entry[1] = max(entry[1], int(total))
            if completed is not None:
                entry[0] = max(entry[0], int(completed))

        total_all = sum(size for _done, size in self.layers.values())
        done_all = sum(min(done, size) for done, size in self.layers.values())

        if total_all:
            if not self.samples or done_all != self.samples[-1][1]:
                self.samples.append((now, done_all))
            while (
                len(self.samples) > 1
                and now - self.samples[0][0] > self.WINDOW_SECONDS
            ):
                self.samples.popleft()

        speed = 0.0
        if len(self.samples) >= 2:
            first_ts, first_done = self.samples[0]
            last_ts, last_done = self.samples[-1]
            span = last_ts - first_ts
            if span >= 0.5:
                speed = max(0.0, (last_done - first_done) / span)

        fraction = (done_all / total_all) if total_all else None
        remaining = max(0, total_all - done_all) if total_all else None
        eta = (remaining / speed) if (remaining is not None and speed > 0) else None
        return {
            "completed": done_all or None,
            "total": total_all or None,
            "fraction": fraction,
            "speed": speed,
            "remaining": remaining,
            "eta": eta,
        }


def _ps_quote(value):
    """Экранирует строку для одинарных кавычек PowerShell."""
    return str(value).replace("'", "''")


def desktop_dir():
    """Путь к рабочему столу (учитывает перенос папки в OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            folder_id = _GUID(
                0xB4BFCC3A, 0xDB2C, 0x424C,
                (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
            )
            path_ptr = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            hr = shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)
            )
            if hr == 0 and path_ptr.value:
                path = str(path_ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                return path
        except Exception:
            pass
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def create_shortcut(lnk_path):
    """Создаёт .lnk на рабочем столе средствами WScript.Shell.

    Возвращает путь к ярлыку или None при ошибке. Цель — собранный .exe, а в
    dev-режиме — pythonw.exe + gui_launcher.py, чтобы ярлык тоже работал.
    """
    if os.name != "nt":
        return None

    if getattr(sys, "frozen", False):
        target = sys.executable
        arguments = ""
        icon = sys.executable
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        target = pythonw if os.path.exists(pythonw) else sys.executable
        arguments = f'"{os.path.join(app_root(), "gui_launcher.py")}"'
        icon = target

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut('{_ps_quote(lnk_path)}')\n"
        f"$sc.TargetPath = '{_ps_quote(target)}'\n"
        f"$sc.Arguments = '{_ps_quote(arguments)}'\n"
        f"$sc.WorkingDirectory = '{_ps_quote(app_root())}'\n"
        f"$sc.IconLocation = '{_ps_quote(icon)}'\n"
        "$sc.Description = 'Гаджет Будущего - переводчик визуальных новелл'\n"
        "$sc.Save()\n"
    )

    import tempfile

    handle, script_path = tempfile.mkstemp(prefix="gadget_lnk_", suffix=".ps1")
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig") as stream:
            stream.write(script)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", script_path,
            ],
            capture_output=True, text=True, timeout=60, creationflags=flags,
        )
        if result.returncode != 0 or not os.path.exists(lnk_path):
            return None
        return lnk_path
    except Exception:
        return None
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


PROVIDER_LABELS = {
    "Ollama (локально)": "ollama",
    "DeepSeek (облако)": "deepseek",
}
PROVIDER_KEYS = {v: k for k, v in PROVIDER_LABELS.items()}

# Доступные модели Ollama: имя -> подпись для выпадающего списка.
OLLAMA_MODELS = {
    "qwen2.5:3b": "qwen2.5:3b (Слабая / Быстрая)",
    "translategemma:4b": "translategemma:4b (Средняя / Баланс)",
    "gemma2:9b": "gemma2:9b (Мощная / Умная)",
}
OLLAMA_MODEL_LABELS = list(OLLAMA_MODELS.values())
MODEL_BY_LABEL = {v: k for k, v in OLLAMA_MODELS.items()}

POSITION_LABELS = {
    "Сверху по центру": "top-center",
    "Снизу по центру": "bottom-center",
    "По центру экрана": "center",
    "Слева сверху": "top-left",
    "Справа сверху": "top-right",
    "Слева снизу": "bottom-left",
    "Справа снизу": "bottom-right",
}
POSITION_KEYS = {v: k for k, v in POSITION_LABELS.items()}


def _is_windows():
    return os.name == "nt"


def _is_elevated():
    """True, если лаунчер запущен с правами администратора."""
    if not _is_windows():
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class TranslatorLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()

        setup_logging()
        self.settings = load_settings()
        self._theme = apply_theme(self.settings.get("launcher_theme", "violet"))

        self.process = None
        self.ollama_pull_proc = None
        self._test_process = None
        self._persist_job = None
        self._log_sig = None

        # --- состояние проверки Ollama и загрузки модели ---
        self._ollama_state = None       # {"cli": bool, "api": bool, "version": str}
        self._ollama_checking = False
        self._ollama_checked_at = 0.0
        self._downloading_model = None
        self._download_cancel = threading.Event()
        self._shortcut_announced = False
        # Плавная полоска загрузки: цель (0..1) и текущее отображаемое значение.
        self._dl_target = 0.0
        self._dl_shown = 0.0
        self._dl_anim_job = None
        # Очередь «сделать в UI-потоке»: фоновые потоки НЕ трогают Tk напрямую
        # (after() из чужого потока может упасть с «main thread is not in main
        # loop»), а кладут сюда колбэк — его выполняет _drain_ui_queue.
        self._ui_queue = queue.Queue()
        self.log = logging.getLogger(__name__)

        self.title(f"Гаджет Будущего — лаунчер v{__version__}")
        self.geometry("640x920")
        self.minsize(600, 720)
        self.configure(fg_color=BG)

        self._build_ui()
        self._apply_settings_to_ui()
        self._refresh_data_location()
        self._refresh_log()
        self._poll_state()
        self._drain_ui_queue()

        # Стартовые проверки — в фоне, чтобы окно открывалось мгновенно.
        self.after(300, self._check_ollama_async)
        self.after(800, self._ensure_shortcut_async)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Карточка перевода — TOPMOST-окно другого процесса. Пока лаунчер в фокусе,
        # поднимаем его выше, чтобы перевод не накрывал кнопки и настройки.
        # Только <Activate>/<Deactivate> (события окна): <FocusIn>/<FocusOut>
        # срабатывают и при переходе фокуса между полями ввода и заставляли окно
        # мигать topmost-состоянием.
        self.bind("<Activate>", self._on_window_activate)
        self.bind("<Deactivate>", self._on_window_deactivate)
        self._topmost_for_focus = False

    def _on_window_activate(self, _event=None):
        try:
            if not self._topmost_for_focus:
                self.attributes("-topmost", True)
                self._topmost_for_focus = True
        except Exception:
            pass

    def _on_window_deactivate(self, _event=None):
        try:
            if self._topmost_for_focus:
                self.attributes("-topmost", False)
                self._topmost_for_focus = False
        except Exception:
            pass

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 6))

        ctk.CTkLabel(
            header, text="ГАДЖЕТ БУДУЩЕГО",
            font=ctk.CTkFont(size=24, weight="bold"), text_color=TEXT,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header, text=f"Локализация визуальных новелл · EN → RU · v{__version__}",
            font=ctk.CTkFont(size=13), text_color=MUTED,
        ).pack(anchor="w", pady=(2, 0))

        # Баннер «Ollama не установлена» живёт в отдельном контейнере между
        # заголовком и телом: так его можно показать/скрыть, не трогая порядок
        # pack-виджетов (у CTkScrollableFrame нельзя указать before=).
        self.banner_host = ctk.CTkFrame(self, fg_color="transparent", height=0)
        self.banner_host.pack(fill="x", padx=24)
        self._build_ollama_banner()

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        self._build_provider_card()
        self._build_download_card()
        self._build_overlay_card()
        self._build_data_card()
        self._build_log_card()
        self._build_footer()

    # -------------------------------------------------- баннер состояния Ollama
    def _build_ollama_banner(self):
        self.ollama_banner = ctk.CTkFrame(
            self.banner_host, fg_color="#2a2110", corner_radius=12,
            border_width=1, border_color=WARN,
        )
        row = ctk.CTkFrame(self.ollama_banner, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=12)

        self.ollama_banner_label = ctk.CTkLabel(
            row, text="", font=ctk.CTkFont(size=12), text_color=TEXT,
            wraplength=380, justify="left",
        )
        self.ollama_banner_label.pack(side="left", fill="x", expand=True)

        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(side="right", padx=(10, 0))

        self.ollama_download_btn = ctk.CTkButton(
            buttons, text="Скачать Ollama", command=self._open_ollama_download,
            height=32, corner_radius=8, width=150,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#ffffff",
        )
        self.ollama_download_btn.pack(pady=(0, 4))

        ctk.CTkButton(
            buttons, text="Проверить снова", command=self._check_ollama_async,
            height=28, corner_radius=8, width=150,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=11), text_color=TEXT,
        ).pack()
        # В дерево не пакуем — появится, когда проверка скажет, что есть проблема.
        self.ollama_banner.pack_forget()

    # ------------------------------------------------------ карточка загрузки
    def _build_download_card(self):
        self.download_card = ctk.CTkFrame(
            self.body, fg_color=CARD, corner_radius=16,
            border_width=1, border_color=ACCENT,
        )

        ctk.CTkLabel(
            self.download_card, text="Загрузка модели",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(14, 6))

        self.dl_status = ctk.CTkLabel(
            self.download_card, text="", font=ctk.CTkFont(size=12),
            text_color=TEXT, anchor="w", justify="left", wraplength=520,
        )
        self.dl_status.pack(fill="x", padx=20)

        self.dl_progress = ctk.CTkProgressBar(
            self.download_card, height=10, corner_radius=6,
            progress_color=ACCENT, fg_color="#2a2a3a", mode="determinate",
        )
        self.dl_progress.pack(fill="x", padx=20, pady=(8, 4))
        self.dl_progress.set(0)

        self.dl_percent = ctk.CTkLabel(
            self.download_card, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.dl_percent.pack(anchor="w", padx=20)

        self.dl_eta = ctk.CTkLabel(
            self.download_card, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.dl_eta.pack(anchor="w", padx=20)

        self.dl_log = ctk.CTkTextbox(
            self.download_card, height=84, fg_color=BG, border_width=1,
            border_color=CARD_BORDER, text_color=MUTED,
            font=ctk.CTkFont(family="Consolas", size=11), wrap="word",
        )
        self.dl_log.pack(fill="x", padx=20, pady=(8, 6))
        self.dl_log.configure(state="disabled")

        row = ctk.CTkFrame(self.download_card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 14))
        self.dl_hint = ctk.CTkLabel(
            row, text="Можно продолжать настройку — загрузка идёт в фоне.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.dl_hint.pack(side="left")
        self.dl_cancel_btn = ctk.CTkButton(
            row, text="Отмена", command=self._cancel_download,
            height=28, corner_radius=8, width=100,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=11), text_color=TEXT, state="disabled",
        )
        self.dl_cancel_btn.pack(side="right")

        self.download_card.pack_forget()

    # ------------------------------------------------------- карточка модели
    def _build_provider_card(self):
        card = self._card("Модель перевода")
        self._provider_card = card

        self.provider_seg = ctk.CTkSegmentedButton(
            card,
            values=list(PROVIDER_LABELS.keys()),
            command=self._on_provider_changed,
            fg_color=BG,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=BG,
            unselected_hover_color="#2a2a3a",
            text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )
        self.provider_seg.pack(fill="x", padx=20)

        self.fields = ctk.CTkFrame(card, fg_color="transparent")
        self.fields.pack(fill="x", padx=20, pady=14)
        self.fields.grid_columnconfigure(1, weight=1)

        self.ollama_model_var = ctk.StringVar(
            value=self.settings.get("ollama_model")
        )
        self.deepseek_model_var = ctk.StringVar(
            value=self.settings.get("deepseek_model")
        )
        self.api_key_var = ctk.StringVar(value=self.settings.get("api_key", ""))
        self.show_key_var = ctk.BooleanVar(value=False)

        self.ollama_label = self._field_label("Модель Ollama")
        self.ollama_menu = ctk.CTkOptionMenu(
            self.fields,
            values=OLLAMA_MODEL_LABELS,
            command=self._on_ollama_model_changed,
            fg_color=BG,
            button_color="#2a2a3a",
            button_hover_color="#3a3a4c",
            dropdown_fg_color=CARD,
            dropdown_hover_color="#2a2a3a",
            text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )
        self._preselect_ollama_model()

        self.deepseek_label = self._field_label("Модель DeepSeek")
        self.deepseek_entry = self._field_entry(self.deepseek_model_var, "deepseek-chat")

        self.api_label = self._field_label("API-ключ DeepSeek")
        self.api_entry = self._field_entry(self.api_key_var, "sk-...", show="*")
        self.api_show = ctk.CTkCheckBox(
            self.fields, text="Показать", variable=self.show_key_var,
            command=self._toggle_key_visibility,
            font=ctk.CTkFont(size=12), text_color=MUTED,
            checkbox_width=16, checkbox_height=16,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        )

        self._grid_field(0, self.ollama_label, self.ollama_menu, None)
        self._grid_field(1, self.deepseek_label, self.deepseek_entry, None)
        self._grid_field(2, self.api_label, self.api_entry, self.api_show)

        self.hint = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=520, justify="left",
        )
        self.hint.pack(fill="x", padx=20, pady=(0, 14))

    # ------------------------------------------------- карточка «Оформление»
    def _build_overlay_card(self):
        # Одна карточка на всё оформление: оверлей, тема и ярлык. Отдельная
        # «Оформление лаунчера» дублировала выбор темы и выглядела как лишний блок.
        card = self._card("Оформление")

        self.overlay_enabled_var = ctk.BooleanVar(value=True)
        self.overlay_source_var = ctk.BooleanVar(value=True)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkSwitch(
            top, text="Показывать перевод в оверлее",
            variable=self.overlay_enabled_var, command=self._schedule_persist,
            font=ctk.CTkFont(size=13), text_color=TEXT,
            progress_color=ACCENT, button_color="#c9c9d6",
        ).pack(anchor="w")

        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=20)
        grid.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            grid, text="Позиция", font=ctk.CTkFont(size=12), text_color=MUTED,
        ).grid(row=0, column=0, sticky="w", pady=4)
        self.position_menu = ctk.CTkOptionMenu(
            grid, values=list(POSITION_LABELS.keys()),
            command=lambda _v: self._schedule_persist(),
            fg_color=BG, button_color="#2a2a3a", button_hover_color="#3a3a4c",
            dropdown_fg_color=CARD, dropdown_hover_color="#2a2a3a",
            text_color=TEXT, font=ctk.CTkFont(size=13),
        )
        self.position_menu.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)

        # Тема — единая настройка: и лаунчер, и карточка перевода.
        ctk.CTkLabel(
            grid, text="Тема", font=ctk.CTkFont(size=12), text_color=MUTED,
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.theme_menu = ctk.CTkOptionMenu(
            grid, values=list(THEME_LABELS.keys()),
            command=self._on_theme_changed,
            fg_color=BG, button_color="#2a2a3a", button_hover_color="#3a3a4c",
            dropdown_fg_color=CARD, dropdown_hover_color="#2a2a3a",
            text_color=TEXT, font=ctk.CTkFont(size=13),
        )
        self.theme_menu.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)

        self.opacity_value = self._slider_row(
            grid, 2, "Прозрачность", 0.3, 1.0, 0.01,
            lambda v: f"{int(round(v * 100))}%",
        )
        self.font_value = self._slider_row(
            grid, 3, "Размер текста", 11, 40, 1, lambda v: f"{int(round(v))} px",
        )
        self.duration_value = self._slider_row(
            grid, 4, "Время показа", 2, 60, 1, lambda v: f"{int(round(v))} с",
        )

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(8, 10))
        ctk.CTkCheckBox(
            bottom, text="Показывать оригинал (EN)", variable=self.overlay_source_var,
            command=self._schedule_persist,
            font=ctk.CTkFont(size=12), text_color=MUTED,
            checkbox_width=16, checkbox_height=16,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).pack(side="left")
        self.test_overlay_btn = ctk.CTkButton(
            bottom, text="Проверить оверлей", command=self.test_overlay,
            height=32, corner_radius=8, width=150,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=12), text_color=TEXT,
        )
        self.test_overlay_btn.pack(side="right")

        # --- ярлык на рабочем столе ---
        self.create_shortcut_var = ctk.BooleanVar(
            value=bool(self.settings.get("create_shortcut", True))
        )
        ctk.CTkCheckBox(
            card, text="Создавать ярлык на рабочем столе при запуске",
            variable=self.create_shortcut_var, command=self._schedule_persist,
            font=ctk.CTkFont(size=12), text_color=MUTED,
            checkbox_width=16, checkbox_height=16,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).pack(anchor="w", padx=20, pady=(0, 4))

        shortcut_row = ctk.CTkFrame(card, fg_color="transparent")
        shortcut_row.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkButton(
            shortcut_row, text="Создать ярлык сейчас", command=self._create_shortcut_now,
            height=30, corner_radius=8, width=180,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=12), text_color=TEXT,
        ).pack(side="left")
        self.shortcut_hint = ctk.CTkLabel(
            shortcut_row, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.shortcut_hint.pack(side="left", padx=(10, 0))

        self.theme_hint = ctk.CTkLabel(
            card, text=(
                "Тема применяется к лаунчеру и к карточке перевода "
                "(карточка перекрасится после перезапуска переводчика)."
            ),
            font=ctk.CTkFont(size=11), text_color=MUTED, wraplength=520, justify="left",
        )
        self.theme_hint.pack(fill="x", padx=20, pady=(0, 14))

    def _slider_row(self, parent, row, title, low, high, step, fmt):
        ctk.CTkLabel(
            parent, text=title, font=ctk.CTkFont(size=12), text_color=MUTED,
        ).grid(row=row, column=0, sticky="w", pady=4)

        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        holder.grid_columnconfigure(0, weight=1)

        value_label = ctk.CTkLabel(
            holder, text="", width=58, anchor="e",
            font=ctk.CTkFont(size=12), text_color=TEXT,
        )
        value_label.grid(row=0, column=1, sticky="e", padx=(8, 0))

        slider = ctk.CTkSlider(
            holder, from_=low, to=high, number_of_steps=int((high - low) / step),
            command=lambda v, sv=step, fv=fmt, vl=value_label: self._on_slider(v, sv, fv, vl),
            progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            fg_color="#2a2a3a",
        )
        slider.grid(row=0, column=0, sticky="ew")
        slider._value_label = value_label
        slider._step = step
        slider._fmt = fmt
        return slider

    def _on_slider(self, value, step, fmt, label):
        snapped = round(float(value) / step) * step
        label.configure(text=fmt(snapped))
        self._schedule_persist()

    # --------------------------------------------------------- карточка данных
    def _build_data_card(self):
        card = self._card("Где лежат данные")
        self.data_label = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=520, justify="left",
        )
        self.data_label.pack(fill="x", padx=20)
        ctk.CTkButton(
            card, text="Открыть папку данных", command=self._open_data_dir,
            height=30, corner_radius=8, width=190,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=12), text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(10, 16))

    # ----------------------------------------------------------- карточка лога
    def _build_log_card(self):
        card = self._card("Журнал переводчика")
        self.log_box = ctk.CTkTextbox(
            card, height=150, fg_color=BG, border_width=1, border_color=CARD_BORDER,
            text_color=MUTED, font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self.log_box.configure(state="disabled")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 14))
        self.log_hint = ctk.CTkLabel(
            row, text="", font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.log_hint.pack(side="left")
        ctk.CTkButton(
            row, text="Открыть файл лога", command=self._open_log_file,
            height=28, corner_radius=8, width=150,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=11), text_color=TEXT,
        ).pack(side="right")

    # --------------------------------------------------------------- подвал
    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color=BG)
        footer.pack(fill="x", padx=24, pady=(6, 18))

        self.status = ctk.CTkLabel(
            footer, text="Готов к запуску.",
            font=ctk.CTkFont(size=12), text_color=MUTED, wraplength=560,
            justify="left",
        )
        self.status.pack(anchor="w", pady=(0, 8))

        buttons = ctk.CTkFrame(footer, fg_color="transparent")
        buttons.pack(fill="x")

        self.launch_btn = ctk.CTkButton(
            buttons, text="Запустить переводчик", command=self.launch,
            height=44, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=15, weight="bold"), text_color="#ffffff",
        )
        self.launch_btn.pack(side="left", fill="x", expand=True)

        self.stop_btn = ctk.CTkButton(
            buttons, text="Остановить", command=self.stop,
            height=44, corner_radius=10, width=120,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=13), text_color=TEXT, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(10, 0))

    def _card(self, title):
        card = ctk.CTkFrame(
            self.body, fg_color=CARD, corner_radius=16,
            border_width=1, border_color=CARD_BORDER,
        )
        card.pack(fill="x", padx=8, pady=6)

        ctk.CTkLabel(
            card, text=title,
            font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(16, 8))
        return card

    # ----------------------------------------------------------- хелперы UI
    def _field_label(self, text):
        return ctk.CTkLabel(
            self.fields, text=text,
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )

    def _field_entry(self, var, placeholder, show=""):
        return ctk.CTkEntry(
            self.fields, textvariable=var, placeholder_text=placeholder,
            height=38, corner_radius=8, show=show,
            fg_color=BG, border_color=CARD_BORDER, border_width=1,
            text_color=TEXT, font=ctk.CTkFont(size=13),
        )

    def _grid_field(self, row, label, entry, extra):
        label.grid(row=row, column=0, sticky="w", pady=6)
        entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=6)
        if extra is not None:
            extra.grid(row=row, column=2, sticky="w", padx=(10, 0))

    # ------------------------------------------------------- настройки -> UI
    def _apply_settings_to_ui(self):
        provider = self.settings.get("provider", "ollama")
        self.provider_seg.set(PROVIDER_KEYS.get(provider, "Ollama (локально)"))
        self._apply_provider(provider)

        self.overlay_enabled_var.set(bool(self.settings.get("overlay_enabled", True)))
        self.overlay_source_var.set(bool(self.settings.get("overlay_show_source", True)))

        position = self.settings.get("overlay_position", "bottom-center")
        self.position_menu.set(POSITION_KEYS.get(position, "Снизу по центру"))

        theme = self.settings.get("launcher_theme", "violet")
        self.theme_menu.set(THEME_KEYS.get(theme, "Фиолетовая"))

        self._set_slider(self.opacity_value, self.settings.get("overlay_opacity", 0.93))
        self._set_slider(self.font_value, self.settings.get("overlay_font_size", 18))
        self._set_slider(self.duration_value, self.settings.get("overlay_duration", 12))

        self.create_shortcut_var.set(bool(self.settings.get("create_shortcut", True)))

        if _is_elevated():
            self._set_status(
                "ВНИМАНИЕ: лаунчер запущен с правами администратора. Из-за UIPI "
                "оверлей не сможет рисовать поверх обычных окон. Перезапусти "
                "лаунчер без прав администратора (обычный двойной клик).",
                kind="warn",
            )

    @staticmethod
    def _set_slider(slider, value):
        try:
            slider.set(float(value))
        except (TypeError, ValueError):
            pass
        label = getattr(slider, "_value_label", None)
        if label is not None:
            label.configure(text=slider._fmt(float(slider.get())))

    def _overlay_settings(self):
        return {
            "overlay_enabled": bool(self.overlay_enabled_var.get()),
            "overlay_show_source": bool(self.overlay_source_var.get()),
            "overlay_position": POSITION_LABELS.get(
                self.position_menu.get(), "bottom-center"
            ),
            "overlay_accent": self._theme,  # единая тема лаунчера и карточки перевода
            "overlay_opacity": round(float(self.opacity_value.get()), 2),
            "overlay_font_size": int(round(float(self.font_value.get()))),
            "overlay_duration": float(round(float(self.duration_value.get()))),
        }

    def _persist_overlay(self):
        """Сохраняет настройки оверлея в settings.json (применятся при запуске)."""
        merged = dict(self.settings)
        merged.update(self._overlay_settings())
        merged.update({
            "provider": self._provider(),
            "api_key": self.api_key_var.get().strip(),
            "ollama_model": self.ollama_model_var.get().strip() or "translategemma:4b",
            "deepseek_model": self.deepseek_model_var.get().strip() or "deepseek-chat",
            "launcher_theme": self._theme,
            "create_shortcut": bool(self.create_shortcut_var.get()),
        })
        try:
            self.settings = save_settings(merged)
        except Exception as exc:
            self._set_status(f"Не удалось сохранить настройки: {exc}", kind="error")

    def _schedule_persist(self, delay_ms=450):
        """Отложенная запись настроек: слайдеры не должны писать файл на каждый пиксель."""
        if self._persist_job is not None:
            try:
                self.after_cancel(self._persist_job)
            except Exception:
                pass
        self._persist_job = self.after(delay_ms, self._persist_now)

    def _persist_now(self):
        self._persist_job = None
        self._persist_overlay()

    # ------------------------------------------------------------- логика
    def _on_provider_changed(self, label):
        self._apply_provider(PROVIDER_LABELS.get(label, "ollama"))
        self._schedule_persist()

    def _apply_provider(self, provider):
        is_deepseek = (provider == "deepseek")

        for w in (
            self.ollama_label, self.ollama_menu,
            self.deepseek_label, self.deepseek_entry,
            self.api_label, self.api_entry, self.api_show,
        ):
            w.grid_remove()

        if is_deepseek:
            self.deepseek_label.grid()
            self.deepseek_entry.grid()
            self.api_label.grid()
            self.api_entry.grid()
            self.api_show.grid()
            self.api_entry.configure(state="normal")
            self.api_show.configure(state="normal")
            self.hint.configure(
                text="DeepSeek работает через облачный API. Вставь свой API-ключ "
                     "(https://platform.deepseek.com). Ключ хранится зашифрованным "
                     "через Windows DPAPI. Лор персонажей пойдёт через эту же модель."
            )
        else:
            self.ollama_label.grid()
            self.ollama_menu.grid()
            self.api_entry.configure(state="disabled")
            self.api_show.configure(state="disabled")
            self.hint.configure(
                text="Ollama работает локально и не требует ключа. Убедись, что "
                     "сервис Ollama запущен (порт 11434). Лор персонажей считает "
                     "эта же модель."
            )

        # Баннер про Ollama: показать/скрыть и при необходимости проверить заново.
        try:
            self._update_ollama_banner()
            if not is_deepseek and self._ollama_state is None:
                self._check_ollama_async()
        except Exception:
            pass

    def _toggle_key_visibility(self):
        self.api_entry.configure(show="" if self.show_key_var.get() else "*")

    def _provider(self):
        return PROVIDER_LABELS.get(self.provider_seg.get(), "ollama")

    def _set_status(self, text, kind="info"):
        color = {"ok": SUCCESS, "error": ERROR, "warn": WARN}.get(kind, MUTED)
        self.status.configure(text=text, text_color=color)

    # ------------------------------------------------ Ollama: модель и скачивание
    def _preselect_ollama_model(self):
        model = self.settings.get("ollama_model", "translategemma:4b")
        label = OLLAMA_MODELS.get(model)
        if label is None:
            model = "translategemma:4b"
            label = OLLAMA_MODELS.get(model, OLLAMA_MODEL_LABELS[0])
        self.ollama_model_var.set(model)
        self.ollama_menu.set(label)

    def _on_ollama_model_changed(self, label):
        model = MODEL_BY_LABEL.get(label)
        if model is None:
            return
        self.ollama_model_var.set(model)
        self._schedule_persist()
        self._set_status("Проверяю Ollama и модель...")
        self._check_ollama_async(
            lambda state: self._after_model_selected(model, state)
        )

    def _after_model_selected(self, model, state):
        if not state.get("api"):
            self._set_status(
                "Ollama не запущена — сначала установи и запусти её.", kind="warn"
            )
            return
        self._check_ollama_model_async(
            model, lambda installed: self._on_model_checked(model, installed)
        )

    def _on_model_checked(self, model, installed):
        if installed:
            self._set_status(f"Модель {model} готова к использованию.", kind="ok")
        else:
            self._set_status(f"Модели {model} нет — начинаю загрузку.", kind="info")
            self._start_model_download()

    # ------------------------------------------------------- Ollama: проверка
    def _ollama_status(self):
        """CLI + сервис Ollama. Блокирующая проверка — вызывать из потока."""
        cli_path = shutil.which("ollama")
        api_ok = False
        version = ""
        try:
            response = requests.get(
                f"{OLLAMA_HOST}/api/version", timeout=OLLAMA_PING_TIMEOUT
            )
            if response.status_code == 200:
                api_ok = True
                version = str((response.json() or {}).get("version") or "")
        except Exception:
            api_ok = False
        return {"cli": cli_path, "api": api_ok, "version": version}

    def _check_ollama_async(self, on_done=None):
        if self._ollama_checking:
            return
        self._ollama_checking = True

        def worker():
            state = self._ollama_status()
            self._ui_call(self._on_ollama_state, state, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ollama_state(self, state, on_done=None):
        self._ollama_checking = False
        self._ollama_state = state
        self._ollama_checked_at = time.time()
        self._update_ollama_banner()
        if callable(on_done):
            on_done(state)

    def _update_ollama_banner(self):
        """Показывает баннер, если для Ollama что-то не готово."""
        state = self._ollama_state
        if self._provider() != "ollama" or not state:
            self.ollama_banner.pack_forget()
            return
        if state.get("api"):
            self.ollama_banner.pack_forget()
            return

        if state.get("cli"):
            text = (
                "Ollama установлена, но сервис не запущен. Запусти Ollama "
                "(из меню «Пуск») и нажми «Проверить снова»."
            )
        else:
            text = (
                "Ollama не установлена. Скачайте для локального перевода. "
                "После установки запусти Ollama и нажми «Проверить снова»."
            )
        self.ollama_banner_label.configure(text=text)
        self.ollama_banner.pack(fill="x", pady=(0, 8))

    def _open_ollama_download(self):
        try:
            webbrowser.open(OLLAMA_DOWNLOAD_URL, new=2)
            self._set_status(
                "Открыл страницу загрузки Ollama. После установки нажми «Проверить снова».",
            )
        except Exception as exc:
            self._set_status(f"Не удалось открыть браузер: {exc}", kind="error")

    # --------------------------------------------------- Ollama: список моделей
    def _ollama_models(self):
        try:
            response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=OLLAMA_MODEL_TIMEOUT)
            if response.status_code != 200:
                return []
            payload = response.json() or {}
            return [
                str(item.get("name") or item.get("model") or "")
                for item in (payload.get("models") or [])
            ]
        except Exception:
            return []

    def _ollama_installed(self, model):
        """True, если модель уже скачана. API — основной путь, CLI — запасной."""
        for name in self._ollama_models():
            if model_names_match(name, model):
                return True
        if self._ollama_state and self._ollama_state.get("api"):
            return False
        try:
            result = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=20
            )
            return model in (result.stdout or "")
        except Exception:
            return False

    def _check_ollama_model_async(self, model, callback):
        def worker():
            installed = self._ollama_installed(model)
            self._ui_call(callback, installed)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------- Ollama: загрузка модели
    def _show_download_card(self, model):
        self._dl_target = 0.0
        self._dl_shown = 0.0
        if self._dl_anim_job is not None:
            try:
                self.after_cancel(self._dl_anim_job)
            except Exception:
                pass
            self._dl_anim_job = None
        self.dl_status.configure(
            text=f"Скачиваю модель {model} — можно продолжать настройку.",
            text_color=TEXT,
        )
        self.dl_percent.configure(text="0%")
        self.dl_eta.configure(text="")
        self.dl_progress.configure(mode="determinate")
        self.dl_progress.set(0)
        self.dl_cancel_btn.configure(state="normal")
        self.dl_hint.configure(text="Можно продолжать настройку — загрузка идёт в фоне.")
        self._dl_clear_log()
        try:
            self.download_card.pack(fill="x", padx=8, pady=6, after=self._provider_card)
            self.body._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass
        self._dl_log(f"Модель: {model}")
        self._dl_log("Соединяюсь с Ollama (POST /api/pull)...")

    def _dl_clear_log(self):
        self.dl_log.configure(state="normal")
        self.dl_log.delete("1.0", "end")
        self.dl_log.configure(state="disabled")

    def _dl_log(self, line):
        if not line:
            return
        try:
            self.dl_log.configure(state="normal")
            self.dl_log.insert("end", f"{time.strftime('%H:%M:%S')}  {line}\n")
            self.dl_log.see("end")
            self.dl_log.configure(state="disabled")
        except Exception:
            pass

    def _dl_tracker_update(self, info):
        """Обновляет цифры прогресса; полоска двигается плавно в _dl_animate."""
        try:
            fraction = info.get("fraction")
            if fraction is None:
                self._dl_target = 0.0
                if str(self.dl_progress.cget("mode")) != "indeterminate":
                    self.dl_progress.configure(mode="indeterminate")
                    self.dl_progress.start()
                self.dl_percent.configure(text="…")
                self.dl_eta.configure(text="")
                return

            fraction = max(0.0, min(1.0, float(fraction)))
            # Только вперёд: когда Ollama объявляет новый слой, знаменатель растёт
            # и «честный» процент падает — визуально это и было дёрганьем.
            self._dl_target = max(self._dl_target, fraction)

            parts = [
                f"{format_bytes(info.get('completed'))} / {format_bytes(info.get('total'))}",
                f"{fraction * 100:.0f}%",
            ]
            if info.get("speed"):
                parts.append(format_speed(info["speed"]))
            self.dl_percent.configure(text="   ·   ".join(parts))

            extra = []
            if info.get("remaining") is not None:
                extra.append(f"осталось {format_bytes(info['remaining'])}")
            if info.get("eta") is not None:
                extra.append(format_duration(info["eta"]))
            self.dl_eta.configure(text="   ·   ".join(extra) if extra else "")

            if self._dl_anim_job is None:
                self._dl_animate()
        except Exception:
            pass

    def _dl_animate(self):
        """Плавно подтягивает полоску к цели (шаг ~80 мс) вместо скачков."""
        try:
            if self._dl_shown < self._dl_target - 0.0005:
                step = max(0.004, (self._dl_target - self._dl_shown) * 0.2)
                self._dl_shown = min(self._dl_target, self._dl_shown + step)
                self.dl_progress.configure(mode="determinate")
                self.dl_progress.set(self._dl_shown)
                self._dl_anim_job = self.after(80, self._dl_animate)
            else:
                self._dl_anim_job = None
        except Exception:
            self._dl_anim_job = None

    def _dl_progress_update(self, status=None, completed=None, total=None):
        try:
            if status:
                self.dl_status.configure(text=status)
            if total:
                fraction = max(0.0, min(1.0, float(completed or 0) / float(total)))
                self.dl_progress.configure(mode="determinate")
                self.dl_progress.set(fraction)
                self.dl_percent.configure(
                    text=(
                        f"{format_bytes(completed)} / {format_bytes(total)}"
                        f"   ·   {fraction * 100:.0f}%"
                    )
                )
            else:
                if str(self.dl_progress.cget("mode")) != "indeterminate":
                    self.dl_progress.configure(mode="indeterminate")
                    self.dl_progress.start()
                self.dl_percent.configure(text="…")
        except Exception:
            pass

    def _start_model_download(self):
        model = self.ollama_model_var.get().strip()
        if not model:
            return
        if self._downloading_model:
            self._set_status(
                f"Уже скачивается модель {self._downloading_model}.", kind="warn"
            )
            return

        state = self._ollama_state
        if not state or not state.get("api"):
            self._set_status(
                "Ollama не запущена: установи/запусти её и нажми «Проверить снова».",
                kind="error",
            )
            self._update_ollama_banner()
            return

        self._downloading_model = model
        self._download_cancel.clear()
        self._show_download_card(model)
        self._set_status(f"Скачивание модели {model}...")

        def worker():
            try:
                self._download_via_api(model)
            except Exception as exc:
                self._ui_call(self._dl_log, f"Загрузка через API не удалась: {exc}")
                self._download_via_cli(model)

        threading.Thread(target=worker, daemon=True).start()

    def _download_via_api(self, model):
        """Потоковая загрузка через Ollama API: прогресс, объём, скорость, ETA."""
        payload = {"model": model, "name": model, "stream": True}
        tracker = DownloadTracker()
        last_status = None
        last_emit = 0.0
        with requests.post(
            f"{OLLAMA_HOST}/api/pull", json=payload, stream=True, timeout=(5, 60)
        ) as response:
            if response.status_code != 200:
                body = (response.text or "")[:200]
                raise RuntimeError(f"HTTP {response.status_code}: {body}")
            for raw in response.iter_lines():
                if self._download_cancel.is_set():
                    self._ui_call(self._dl_log, "Отменено пользователем.")
                    self._ui_call(self._finish_download, False, "Загрузка отменена.")
                    return
                if not raw:
                    continue
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))

                status = str(event.get("status") or "")
                digest = str(event.get("digest") or "")
                info = tracker.update(
                    digest=digest,
                    completed=event.get("completed"),
                    total=event.get("total"),
                )
                if status and status != last_status:
                    last_status = status
                    self._ui_call(self._dl_log, status)

                # Обновляем полоску не чаще 5 раз в секунду, иначе очередь
                # UI-колбэков забивается тысячами событий Ollama.
                now = time.monotonic()
                if now - last_emit >= 0.2:
                    last_emit = now
                    self._ui_call(self._dl_tracker_update, info)

        self._ui_call(self._dl_log, "Загрузка завершена.")
        self._ui_call(self._finish_download, True, "")

    def _download_via_cli(self, model):
        """Запасной путь: `ollama pull` без API (прогресс — по списку моделей)."""
        executable = shutil.which("ollama")
        if not executable:
            self._ui_call(self._finish_download, False, "Ollama не найдена — установите её.")
            return
        self._ui_call(self._dl_log, "Пробую запасной путь: ollama pull (CLI)...")
        self._ui_call(self._dl_progress_update, "Загрузка через CLI...", None, None)
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [executable, "pull", model],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            self.ollama_pull_proc = proc
            while proc.poll() is None:
                if self._download_cancel.is_set():
                    proc.terminate()
                    break
                time.sleep(3)
                if self._ollama_installed(model):
                    break
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

            if self._download_cancel.is_set():
                self._ui_call(self._finish_download, False, "Загрузка отменена.")
                return
            if self._ollama_installed(model):
                self._ui_call(self._dl_log, "Модель появилась в списке Ollama.")
                self._ui_call(self._finish_download, True, "")
            else:
                self.after(
                    0, self._finish_download, False,
                    "Не удалось скачать модель через CLI — проверь журнал.",
                )
        except Exception as exc:
            self._ui_call(self._finish_download, False, f"Ошибка CLI-загрузки: {exc}")
        finally:
            self.ollama_pull_proc = None

    def _cancel_download(self):
        if not self._downloading_model:
            return
        self._download_cancel.set()
        self.dl_cancel_btn.configure(state="disabled")
        self.dl_status.configure(text="Останавливаю загрузку...")

    def _finish_download(self, ok, message=""):
        model = self._downloading_model or self.ollama_model_var.get().strip()
        self._downloading_model = None
        self._download_cancel.clear()
        if self._dl_anim_job is not None:
            try:
                self.after_cancel(self._dl_anim_job)
            except Exception:
                pass
            self._dl_anim_job = None
        self._dl_target = 1.0 if ok else self._dl_shown
        try:
            self.dl_progress.stop()
        except Exception:
            pass
        try:
            self.dl_progress.configure(mode="determinate")
            self.dl_cancel_btn.configure(state="disabled")
        except Exception:
            pass

        if ok:
            self._dl_shown = 1.0
            self.dl_progress.set(1.0)
            self.dl_percent.configure(text="100% · готово")
            self.dl_eta.configure(text="")
            self.dl_status.configure(text=f"Модель {model} скачана.", text_color=SUCCESS)
            self.dl_hint.configure(text="Теперь можно нажать «Запустить переводчик».")
            self._dl_log("Готово.")
            self._set_status(
                f"Модель {model} готова. Нажми «Запустить переводчик».", kind="ok"
            )
        else:
            self.dl_eta.configure(text="")
            self.dl_status.configure(
                text=message or "Загрузка не удалась.", text_color=ERROR
            )
            self.dl_hint.configure(text="Проверь консоль выше и попробуй ещё раз.")
            self._set_status(message or "Не удалось скачать модель.", kind="error")

    # ------------------------------------------------------------ оформление
    def _on_theme_changed(self, label):
        if self._downloading_model:
            self._set_status("Дождись окончания загрузки — потом сменим тему.", kind="warn")
            try:
                self.theme_menu.set(THEME_KEYS.get(self._theme, "Фиолетовая"))
            except Exception:
                pass
            return

        name = THEME_LABELS.get(label, "violet")
        if name == self._theme:
            return

        self._persist_now()  # сначала сохраняем текущие правки
        merged = dict(self.settings)
        merged["launcher_theme"] = name
        try:
            self.settings = save_settings(merged)
        except Exception as exc:
            self._set_status(f"Не удалось сохранить тему: {exc}", kind="error")
            return

        self._rebuild_ui()
        self._set_status(f"Тема «{label}» применена.", kind="ok")

    def _rebuild_ui(self):
        """Пересобирает окно после смены палитры."""
        self._theme = apply_theme(self.settings.get("launcher_theme", "violet"))
        for child in self.winfo_children():
            child.destroy()
        self.configure(fg_color=BG)
        self._build_ui()
        self._apply_settings_to_ui()
        self._refresh_data_location()
        self._refresh_log()
        self._update_ollama_banner()
        try:
            self.update_idletasks()
            self.body._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ ярлык
    def _shortcut_path(self):
        return os.path.join(desktop_dir(), "Гаджет Будущего.lnk")

    def _ensure_shortcut_async(self):
        """Создаёт ярлык при первом запуске собранного exe (если включено)."""
        if not bool(self.settings.get("create_shortcut", True)):
            return
        if not getattr(sys, "frozen", False):
            return
        lnk = self._shortcut_path()
        if os.path.exists(lnk):
            return

        def worker():
            path = create_shortcut(lnk)
            self._ui_call(self._on_shortcut_created, path, False)

        threading.Thread(target=worker, daemon=True).start()

    def _create_shortcut_now(self):
        lnk = self._shortcut_path()
        try:
            self.shortcut_hint.configure(text="Создаю ярлык...", text_color=MUTED)
        except Exception:
            pass

        def worker():
            path = create_shortcut(lnk)
            self._ui_call(self._on_shortcut_created, path, True)

        threading.Thread(target=worker, daemon=True).start()

    def _on_shortcut_created(self, path, manual):
        if path:
            try:
                self.shortcut_hint.configure(
                    text="Ярлык на рабочем столе создан.", text_color=SUCCESS
                )
            except Exception:
                pass
            if manual:
                self._set_status("Ярлык создан на рабочем столе.", kind="ok")
        else:
            try:
                self.shortcut_hint.configure(
                    text="Не удалось создать ярлык.", text_color=ERROR
                )
            except Exception:
                pass
            if manual:
                self._set_status(
                    "Не удалось создать ярлык. Проверь, что рабочий стол доступен "
                    "на запись и что PowerShell не заблокирован политикой.",
                    kind="error",
                )

    # --------------------------------------------------------- запуск/остановка
    def _hidden_flags(self):
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _spawn_process(self, cmd):
        try:
            self.process = subprocess.Popen(
                cmd, cwd=app_root(), creationflags=self._hidden_flags()
            )
        except Exception as e:
            self._set_status(f"Ошибка запуска: {e}", kind="error")
            return False
        self.launch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status(
            "Переводчик запущен БЕЗ прав администратора. Win+Shift+S — скриншот, "
            "F6 — повторить перевод, F7 — скрыть оверлей, F8 — пауза, F9 — очистить память.",
            kind="ok",
        )
        self._scroll_to_log()
        return True

    def _scroll_to_log(self):
        """Прокручивает лаунчер к карточке журнала после запуска."""
        try:
            self.update_idletasks()
            self.body._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _launch_with_model(self, cmd, installed):
        model = self.ollama_model_var.get().strip()
        if not installed:
            self._set_status(
                f"Модели {model} нет — запускаю загрузку. После окончания нажми "
                "«Запустить» ещё раз.",
                kind="warn",
            )
            self._start_model_download()
            return
        self._spawn_process(cmd)

    def launch(self):
        if self.process and self.process.poll() is None:
            self._set_status("Переводчик уже запущен.", kind="warn")
            return

        provider = self._provider()
        api_key = self.api_key_var.get().strip()

        if provider == "deepseek" and not api_key:
            self._set_status("Для DeepSeek нужен API-ключ.", kind="error")
            return

        self._persist_overlay()

        cmd = self._build_translator_command()
        if not cmd:
            self._set_status("Не найден main.py или GadgetOfTheFuture.exe.", kind="error")
            return

        if self.settings.get("overlay_enabled", True) is False:
            self._set_status(
                "Оверлей выключен в настройках — перевод будет только в журнале.",
                kind="warn",
            )

        if provider == "ollama":
            state = self._ollama_state
            if state is None:
                # Ещё не проверяли (или проверка ещё идёт) — не блокируем UI.
                self._set_status("Проверяю Ollama, нажми «Запустить» ещё раз.", kind="warn")
                self._check_ollama_async()
                return
            if not state.get("api"):
                self._update_ollama_banner()
                self._set_status(
                    "Ollama не запущена. Установи её (кнопка «Скачать Ollama») или "
                    "запусти сервис и нажми «Проверить снова».",
                    kind="error",
                )
                return
            self._set_status("Проверка модели...")
            self._check_ollama_model_async(
                self.settings.get("ollama_model", ""),
                lambda installed: self._launch_with_model(cmd, installed),
            )
            return

        self._spawn_process(cmd)

    def test_overlay(self):
        """Показывает тестовую карточку оверлея (отдельный короткий процесс)."""
        if self._test_process and self._test_process.poll() is None:
            return
        self._persist_overlay()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--overlay-test", "--seconds", "5"]
        else:
            cmd = [sys.executable, os.path.join(app_root(), "main.py"),
                   "--overlay-test", "--seconds", "5"]
        try:
            self._test_process = subprocess.Popen(
                cmd, cwd=app_root(), creationflags=self._hidden_flags()
            )
            self.test_overlay_btn.configure(state="disabled")
            self._set_status(
                "Тестовый оверлей показан на 5 секунд. Если его не видно — "
                "проверь, не запущен ли лаунчер с правами администратора.",
            )
            self.after(6000, lambda: self.test_overlay_btn.configure(state="normal"))
        except Exception as exc:
            self._set_status(f"Не удалось показать оверлей: {exc}", kind="error")

    def _terminate_process(self):
        proc = self.process
        self.process = None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    def stop(self):
        self._terminate_process()
        self.launch_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._set_status("Остановлено.", kind="ok")

    def _on_close(self):
        if self._downloading_model:
            self._download_cancel.set()
            proc = self.ollama_pull_proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._terminate_process()
        if self._test_process and self._test_process.poll() is None:
            try:
                self._test_process.terminate()
            except Exception:
                pass
        self.destroy()

    def _build_translator_command(self):
        if getattr(sys, "frozen", False):
            # В собранном .exe нет отдельного main.py — запускаем самих себя
            # с флагом --run-translator (main.py при импорте начнёт перевод).
            return [sys.executable, "--run-translator"]
        main_py = os.path.join(app_root(), "main.py")
        if not os.path.exists(main_py):
            return None
        return [sys.executable, main_py]

    # ------------------------------------------------------------- данные/лог
    def _refresh_data_location(self):
        try:
            hint = data_location_hint()
        except Exception as exc:
            hint = f"(не удалось определить: {exc})"
        self.data_label.configure(
            text="Память диалога, персонажи, глоссарий, настройки и журнал "
                 f"лежат здесь: {hint}"
        )
        self.log_hint.configure(text=os.path.basename(log_file_path()))

    def _open_data_dir(self):
        from brain.paths import ensure_data_dir

        path = ensure_data_dir()
        try:
            os.startfile(path)  # noqa: S606  (Открыть папку в Проводнике)
        except Exception as exc:
            self._set_status(f"Не удалось открыть папку: {exc}", kind="error")

    def _open_log_file(self):
        path = log_file_path()
        try:
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            self._set_status(f"Не удалось открыть лог: {exc}", kind="error")

    def _read_log_tail(self, lines=40):
        path = log_file_path()
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:]).strip()
        except OSError:
            return ""

    def _refresh_log(self):
        text = self._read_log_tail()
        signature = (len(text), text[-80:])
        if signature == self._log_sig:
            return
        self._log_sig = signature
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("1.0", text or "Журнал пока пуст. Запусти переводчик.")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def _poll_state(self):
        """Обновляет журнал и следит за завершением процесса."""
        self._refresh_log()
        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            self.launch_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self._set_status(
                f"Переводчик завершился (код {code}). Подробности — в журнале.",
                kind="warn" if code else "info",
            )
        try:
            self.after(1200, self._poll_state)
        except Exception:
            pass

    # ------------------------------------------------ безопасные UI-колбэки
    def _ui_call(self, fn, *args):
        """Ставит колбэк в очередь: выполнится в UI-потоке (см. _drain_ui_queue)."""
        try:
            self._ui_queue.put((fn, args))
        except Exception:
            pass

    def _drain_ui_queue(self):
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                self.log.exception("Ошибка UI-колбэка из фонового потока")
        try:
            self.after(80, self._drain_ui_queue)
        except Exception:
            pass


if __name__ == "__main__":
    app = TranslatorLauncher()
    app.mainloop()
