# gui_launcher.py
"""Графический лаунчер «Гаджет Будущего» v1.4.2.

Что изменилось в v1.4.2 (почему переписан интерфейс):

* Переводчик больше не печатает перевод в консоль: он показывает его в
  оверлее поверх игры (brain/overlay.py). Лаунчер запускает его без окна
  консоли и показывает живой лог прямо у себя.
* Лаунчер больше НЕ запрашивает права администратора. Запуск с правами
  администратора включал UIPI и делал оверлей невидимым поверх обычных окон,
  поэтому приложение работает с обычными правами (asInvoker).
* Появилась карточка настроек оверлея: позиция, прозрачность, размер шрифта,
  время показа, цвет и показ оригинала.
* Видно, где лежат данные (память, настройки, лог) и есть кнопка, чтобы
  открыть эту папку — раньше было непонятно, куда всё сохраняется.
"""

import os
import subprocess
import sys
import threading

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
from brain.settings import load_settings, save_settings
from brain.version import __version__

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# --- Палитра тёмной темы ---
BG = "#12121a"
CARD = "#1c1c28"
CARD_BORDER = "#2a2a3a"
TEXT = "#e6e6ef"
MUTED = "#8a8aa0"
ACCENT = "#7c3aed"
ACCENT_HOVER = "#8f52ff"
SUCCESS = "#4ade80"
ERROR = "#f87171"
WARN = "#fbbf24"

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

ACCENT_LABELS = {
    "Фиолетовый": "violet",
    "Синий": "blue",
    "Зелёный": "green",
    "Оранжевый": "orange",
}
ACCENT_KEYS = {v: k for k, v in ACCENT_LABELS.items()}


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
        self.process = None
        self.ollama_pull_proc = None
        self._test_process = None
        self._persist_job = None
        self._log_sig = None

        self.title(f"Гаджет Будущего — лаунчер v{__version__}")
        self.geometry("640x920")
        self.minsize(600, 720)
        self.configure(fg_color=BG)

        self._build_ui()
        self._apply_settings_to_ui()
        self._refresh_data_location()
        self._refresh_log()
        self._poll_state()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        self._build_provider_card()
        self._build_overlay_card()
        self._build_data_card()
        self._build_log_card()
        self._build_footer()

    # ------------------------------------------------------- карточка модели
    def _build_provider_card(self):
        card = self._card("Модель перевода")

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

    # ------------------------------------------------------- карточка оверлея
    def _build_overlay_card(self):
        card = self._card("Оверлей перевода")

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

        ctk.CTkLabel(
            grid, text="Цвет", font=ctk.CTkFont(size=12), text_color=MUTED,
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.accent_menu = ctk.CTkOptionMenu(
            grid, values=list(ACCENT_LABELS.keys()),
            command=lambda _v: self._schedule_persist(),
            fg_color=BG, button_color="#2a2a3a", button_hover_color="#3a3a4c",
            dropdown_fg_color=CARD, dropdown_hover_color="#2a2a3a",
            text_color=TEXT, font=ctk.CTkFont(size=13),
        )
        self.accent_menu.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)

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
        bottom.pack(fill="x", padx=20, pady=(8, 16))
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

        accent = self.settings.get("overlay_accent", "violet")
        self.accent_menu.set(ACCENT_KEYS.get(accent, "Фиолетовый"))

        self._set_slider(self.opacity_value, self.settings.get("overlay_opacity", 0.93))
        self._set_slider(self.font_value, self.settings.get("overlay_font_size", 18))
        self._set_slider(self.duration_value, self.settings.get("overlay_duration", 12))

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
            "overlay_accent": ACCENT_LABELS.get(self.accent_menu.get(), "violet"),
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
                     "через Windows DPAPI."
            )
        else:
            self.ollama_label.grid()
            self.ollama_menu.grid()
            self.api_entry.configure(state="disabled")
            self.api_show.configure(state="disabled")
            self.hint.configure(
                text="Ollama работает локально и не требует ключа. Убедись, что "
                     "сервис Ollama запущен (порт 11434)."
            )

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
        self._set_status("Проверка модели...")
        self._check_ollama_model_async(model, self._on_ollama_model_check_done)

    def _ollama_installed(self, model):
        try:
            result = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=15
            )
            return model in (result.stdout or "")
        except Exception:
            return False

    def _check_ollama_model_async(self, model, callback):
        def worker():
            installed = self._ollama_installed(model)
            self.after(0, callback, installed)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ollama_model_check_done(self, installed):
        if installed:
            self._set_status("Модель готова к использованию.", kind="ok")
        else:
            self._start_model_download()

    def _start_model_download(self):
        model = self.ollama_model_var.get().strip()
        if not model:
            return
        self._set_status(f"Скачивание модели {model}...")
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.ollama_pull_proc = subprocess.Popen(
                ["ollama", "pull", model], creationflags=flags
            )
        except Exception as e:
            self._set_status(f"Ошибка скачивания: {e}", kind="error")

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
            self._start_model_download()
            self._set_status(
                f"Скачивание модели {model}... Дождитесь окончания и нажмите «Запустить» ещё раз."
            )
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
        self.after(1200, self._poll_state)


if __name__ == "__main__":
    app = TranslatorLauncher()
    app.mainloop()
