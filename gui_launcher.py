# gui_launcher.py
"""Графический лаунчер «Гаджет Будущего».

Позволяет выбрать провайдера перевода (локальная Ollama или облачный DeepSeek),
ввести API-ключ и запустить main.py в фоновом процессе. Настройки сохраняются
в data/settings.json и подхватываются brain/llm_engine.py.
"""

import os
import subprocess
import sys
import threading

# Гарантируем, что корень проекта в sys.path (нужно для импорта пакета brain).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import customtkinter as ctk
except ImportError:
    sys.stderr.write(
        "Не установлен CustomTkinter.\n"
        "Установите его командой:  pip install customtkinter\n"
    )
    sys.exit(1)

from brain.paths import app_root
from brain.settings import DEFAULT_SETTINGS, load_settings, save_settings

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


class TranslatorLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.settings = load_settings()
        self.process = None
        self.ollama_pull_proc = None

        self.title("Лаборатория Гаджетов Будущего — Запуск переводчика")
        self.geometry("500x560")
        self.minsize(500, 560)
        self.configure(fg_color=BG)

        self._build_ui()

        provider = self.settings.get("provider", "ollama")
        self.provider_seg.set(PROVIDER_KEYS.get(provider, "Ollama (локально)"))
        self._apply_provider(provider)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(24, 8))

        ctk.CTkLabel(
            header, text="ГАДЖЕТ БУДУЩЕГО",
            font=ctk.CTkFont(size=24, weight="bold"), text_color=TEXT,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header, text="Локализация визуальных новелл · EN → RU",
            font=ctk.CTkFont(size=13), text_color=MUTED,
        ).pack(anchor="w", pady=(2, 0))

        card = ctk.CTkFrame(
            self, fg_color=CARD, corner_radius=16,
            border_width=1, border_color=CARD_BORDER,
        )
        card.pack(fill="both", expand=True, padx=24, pady=8)

        ctk.CTkLabel(
            card, text="Модель перевода",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(20, 8))

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
        self.fields.pack(fill="x", padx=20, pady=16)

        self.ollama_model_var = ctk.StringVar(
            value=self.settings.get("ollama_model", DEFAULT_SETTINGS["ollama_model"])
        )
        self.deepseek_model_var = ctk.StringVar(
            value=self.settings.get("deepseek_model", DEFAULT_SETTINGS["deepseek_model"])
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
        self.api_entry.bind("<Control-v>", self._paste_api_key)
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
            card, text="",
            font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=430, justify="left",
        )
        self.hint.pack(fill="x", padx=20, pady=(0, 16))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 20))

        self.status = ctk.CTkLabel(
            footer, text="Готов к запуску.",
            font=ctk.CTkFont(size=12), text_color=MUTED, wraplength=440,
        )
        self.status.pack(anchor="w", pady=(0, 10))

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
            height=44, corner_radius=10, width=110,
            fg_color="#2a2a3a", hover_color="#3a3a4c",
            font=ctk.CTkFont(size=13), text_color=TEXT, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(10, 0))

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
        self.fields.grid_columnconfigure(1, weight=1)

    # ------------------------------------------------------------- логика
    def _on_provider_changed(self, label):
        self._apply_provider(PROVIDER_LABELS.get(label, "ollama"))

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
                     "(https://platform.deepseek.com)."
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
        color = {"ok": SUCCESS, "error": ERROR}.get(kind, MUTED)
        self.status.configure(text=text, text_color=color)

    # ------------------------------------------------ Ollama: модель и скачивание
    def _preselect_ollama_model(self):
        """Ставит в выпадающий список сохранённую модель (или значение по умолчанию)."""
        model = self.settings.get("ollama_model", DEFAULT_SETTINGS["ollama_model"])
        label = OLLAMA_MODELS.get(model)
        if label is None:
            model = DEFAULT_SETTINGS["ollama_model"]
            label = OLLAMA_MODELS.get(model, OLLAMA_MODEL_LABELS[0])
        self.ollama_model_var.set(model)
        self.ollama_menu.set(label)

    def _on_ollama_model_changed(self, label):
        model = MODEL_BY_LABEL.get(label)
        if model is None:
            return
        self.ollama_model_var.set(model)
        self._set_status("Проверка модели...")
        self._check_ollama_model_async(model, self._on_ollama_model_check_done)

    def _ollama_installed(self, model):
        """True, если модель уже скачана в Ollama."""
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
            self.ollama_pull_proc = subprocess.Popen(["ollama", "pull", model], creationflags=flags)
        except Exception as e:
            self._set_status(f"Ошибка скачивания: {e}", kind="error")

    def _paste_api_key(self, event=None):
        try:
            text = self.clipboard_get()
        except Exception:
            return "break"
        self.api_entry.insert("end", text)
        return "break"

    # ------------------------------------------------------------ запуск/остановка
    def _spawn_process(self, cmd, env):
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(cmd, cwd=app_root(), env=env, creationflags=flags)
        except Exception as e:
            self._set_status(f"Ошибка запуска: {e}", kind="error")
            return
        self.launch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status(
            "Переводчик запущен. Win+Shift+S — перевести, F8 — пауза, F9 — очистить память.",
            kind="ok",
        )
        self.iconify()

    def _launch_with_model(self, cmd, env, installed):
        model = env.get("GADGET_OLLAMA_MODEL", "")
        if not installed:
            self._start_model_download()
            self._set_status(
                f"Скачивание модели {model}... Дождитесь окончания и нажмите «Запустить» ещё раз."
            )
            return
        self._spawn_process(cmd, env)

    def launch(self):
        if self.process and self.process.poll() is None:
            self._set_status("Переводчик уже запущен.")
            return

        provider = self._provider()
        api_key = self.api_key_var.get().strip()

        if provider == "deepseek" and not api_key:
            self._set_status("Для DeepSeek нужен API-ключ.", kind="error")
            return

        settings = {
            "provider": provider,
            "ollama_model": self.ollama_model_var.get().strip() or DEFAULT_SETTINGS["ollama_model"],
            "deepseek_model": self.deepseek_model_var.get().strip() or DEFAULT_SETTINGS["deepseek_model"],
            "api_key": api_key,
        }
        self.settings = save_settings(settings)

        cmd = self._build_command()
        if not cmd:
            self._set_status("Не найден main.py или GadgetOfTheFuture.exe.", kind="error")
            return

        env = os.environ.copy()
        env["GADGET_PROVIDER"] = provider
        env["GADGET_API_KEY"] = api_key
        env["GADGET_OLLAMA_MODEL"] = settings["ollama_model"]
        env["GADGET_DEEPSEEK_MODEL"] = settings["deepseek_model"]

        if provider == "ollama":
            self._set_status("Проверка модели...")
            self._check_ollama_model_async(
                settings["ollama_model"],
                lambda installed: self._launch_with_model(cmd, env, installed),
            )
            return

        self._spawn_process(cmd, env)

    def _terminate_process(self):
        """Отправляет terminate(), а при необходимости — kill() фоновому процессу."""
        proc = self.process
        self.process = None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
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
        self.deiconify()

    def _on_close(self):
        """Обработчик закрытия окна: не оставляем main.py висеть в фоне."""
        self._terminate_process()
        self.destroy()

    def _build_command(self):
        root = app_root()
        main_py = os.path.join(root, "main.py")
        if os.path.exists(main_py):
            return [sys.executable, main_py]
        exe = os.path.join(root, "GadgetOfTheFuture.exe")
        if os.path.exists(exe):
            return [exe]
        return None


if __name__ == "__main__":
    app = TranslatorLauncher()
    app.mainloop()
