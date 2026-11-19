# brain/ocr_providers/win_ocr.py
"""Windows Native OCR — распознавание текста штатным движком Windows.

Зачем: EasyOCR тянет за собой torch (~2.5 ГБ в сборке) и на CPU работает
секундами. Windows.Media.Ocr — системный движок (появился в Windows 10),
работает за ~10-30 мс, ничего не скачивает и не требует GPU.

Публичный интерфейс намеренно повторяет EasyOCR, чтобы не переписывать
main.py:

    ocr = WinOCR()
    ocr.read_text(image_path_or_pil)   # -> str  (основной метод)
    ocr.extract_text(image)            # -> str  (как brain.ocr_engine.OCREngine)
    ocr.readtext(image, detail=0)      # -> ["строка", ...]  (как easyocr.Reader.readtext)
    ocr.readtext(image, detail=1)      # -> [([[x,y],...], "строка", 0.9), ...]

Два бэкенда:
1. "winrt" — пакеты ``winrt-*`` (pywinrt). Основной, быстрый, без подпроцессов.
2. "powershell" — запасной: тот же Windows.Media.Ocr, но через Windows
   PowerShell 5.1. Нужен, если пакеты winrt не встали (например, для новой
   версии Python ещё нет колеса winrt-runtime) или если сборка PyInstaller
   не подхватила нативные модули winrt.

Требования к системе: Windows 10+ и установленный языковой пакет OCR.
Если пакета en-US нет, движок пробует языки пользовательского профиля и
первый доступный — список задаётся в ``WinOCR(languages=...)``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

log = logging.getLogger(__name__)

# Тип входа: путь к файлу, PIL.Image, «сырые» байты или numpy-массив.
ImageInput = Union[str, "os.PathLike[str]", bytes, bytearray, Any]

# EasyOCR: детальный элемент — (bbox_4_points, text, confidence).
DetailItem = Tuple[List[List[int]], str, float]

# Минимальный максимальный размер стороны после апскейла: мелкий текст
# (13-15 px, как в визуальных новеллах) системный движок разбирает заметно
# хуже, а после увеличения в 2 раза — почти без ошибок в пунктуации.
_MIN_SIDE_TARGET = 1000
_MAX_UPSCALE = 2.0

# Бэкенды и правила деградации. Один неудачный кадр не должен навсегда
# переключать OCR на медленный путь: бэкенд уходит в «отпуск» только после
# _FAILURE_THRESHOLD ошибок подряд и возвращается через _BACKEND_COOLDOWN секунд.
BACKEND_AUTO = "auto"
BACKEND_WINRT = "winrt"
BACKEND_POWERSHELL = "powershell"
_BACKENDS = (BACKEND_AUTO, BACKEND_WINRT, BACKEND_POWERSHELL)
_FAILURE_THRESHOLD = 3
_BACKEND_COOLDOWN = 30.0
_ASYNC_TIMEOUT = 20.0


class OcrUnavailableError(RuntimeError):
    """Ни один бэкенд Windows OCR не доступен на этой машине."""


def _pil_from_input(image: ImageInput):
    """Приводит любой поддерживаемый вход к PIL.Image в режиме RGB."""
    from PIL import Image  # локально: модуль может импортироваться без Pillow

    if isinstance(image, Image.Image):
        return image if image.mode == "RGB" else image.convert("RGB")

    if isinstance(image, (bytes, bytearray)):
        # with + load(): Image.open ленив, и без этого дескриптор файла живёт
        # до сборщика мусора (на Windows файл всё это время залочен).
        with Image.open(io.BytesIO(bytes(image))) as opened:
            opened.load()
            return opened.convert("RGB")

    if isinstance(image, (str, os.PathLike)):
        with Image.open(os.fspath(image)) as opened:
            opened.load()
            return opened.convert("RGB")

    # numpy-массив (так кадр приходил в EasyOCR) или что-то array-like.
    # numpy НЕ входит в lightweight-сборку, поэтому в exe этот путь работает
    # только если numpy поставили отдельно.
    fromarray = getattr(Image, "fromarray", None)
    if fromarray is None:
        raise TypeError("Image.fromarray недоступен: установите numpy или передайте PIL.Image")
    img = fromarray(image)
    return img if img.mode == "RGB" else img.convert("RGB")


def _prepare_image(img, upscale: bool, max_width: Optional[int] = None):
    """Апскейл мелких кадров и даунскейл слишком больших (лимит движка)."""
    width, height = img.size
    if width <= 0 or height <= 0:
        return img

    limit = 10000  # Ограничение Windows.Media.Ocr: 10000 px по стороне.
    if max_width and 0 < int(max_width) < limit:
        limit = int(max_width)

    longest = max(width, height)
    if longest > limit:
        scale = limit / float(longest)
        img = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            _resample(),
        )
        width, height = img.size
        longest = max(width, height)

    if upscale and longest < _MIN_SIDE_TARGET:
        scale = min(_MAX_UPSCALE, _MIN_SIDE_TARGET / float(longest))
        if scale > 1.05:
            img = img.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                _resample(),
            )
    return img


def _resample():
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _init_apartment():
    """Инициализирует COM/WinRT apartment в текущем потоке (если нужно).

    pywinrt сам инициализирует apartment главного потока, но для потоков,
    которые мы создаём вручную (см. WinOCR._run_async), это нужно сделать
    явно — иначе создание WinRT-объектов падает.
    """
    try:
        import winrt

        init = getattr(winrt, "init_apartment", None)
        if init is not None:
            init()
    except Exception:
        pass


def _uninit_apartment():
    try:
        import winrt

        uninit = getattr(winrt, "uninit_apartment", None)
        if uninit is not None:
            uninit()
    except Exception:
        pass


class WinOCR:
    """OCR через системный движок Windows; интерфейс совместим с EasyOCR."""

    def __init__(
        self,
        languages: Optional[Sequence[str]] = None,
        backend: str = "auto",
        upscale: bool = True,
        max_width: Optional[int] = None,
    ):
        backend = (backend or BACKEND_AUTO).lower()
        if backend not in _BACKENDS:
            raise ValueError(
                "Неизвестный бэкенд OCR %r; ожидается один из %s" % (backend, ", ".join(_BACKENDS))
            )

        self.languages = tuple(str(tag) for tag in (languages or ("en-US", "en", "ru")))
        self.backend = backend
        self.upscale = bool(upscale)
        self.max_width = max_width

        # Последняя ошибка распознавания — чтобы main мог показать её в карточке,
        # а не просто «текст не распознан».
        self.last_error: Optional[BaseException] = None

        self._engine = None
        self._language_tag: Optional[str] = None
        self._active_backend: Optional[str] = None
        self._lock = threading.RLock()
        self._failures = {BACKEND_WINRT: 0, BACKEND_POWERSHELL: 0}
        self._cooldown_until = {BACKEND_WINRT: 0.0, BACKEND_POWERSHELL: 0.0}
        self._reported_failure = {BACKEND_WINRT: False, BACKEND_POWERSHELL: False}

    # ------------------------------------------------------------------
    # служебное
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> Optional[str]:
        """Фактически используемый бэкенд: "winrt" | "powershell" | None."""
        return self._active_backend

    @property
    def language_tag(self) -> Optional[str]:
        """Язык, на котором реально распознаёт движок."""
        return self._language_tag

    @staticmethod
    def available_recognizer_languages() -> List[str]:
        """Языки OCR, установленные в системе (пустой список при сбое)."""
        try:
            from winrt.windows.media.ocr import OcrEngine

            return [str(item.language_tag) for item in OcrEngine.available_recognizer_languages]
        except Exception:
            return []

    @classmethod
    def is_available(cls) -> bool:
        """True, если распознавание в принципе заработает на этой машине."""
        try:
            if cls.available_recognizer_languages():
                return True
        except Exception:
            pass
        return _powershell_ocr_available()

    # ------------------------------------------------------------------
    # публичный API (EasyOCR-совместимый)
    # ------------------------------------------------------------------

    def read_text(self, image_path_or_pil: ImageInput) -> str:
        """Возвращает распознанный текст одной строкой ('' при неудаче)."""
        return " ".join(item[1] for item in self.read_items(image_path_or_pil)).strip()

    # Псевдонимы, чтобы модуль можно было подставить вместо brain.ocr_engine.
    def extract_text(self, image: ImageInput) -> str:
        return self.read_text(image)

    def readtext(self, image: ImageInput, detail: int = 0, **_kwargs) -> List[Any]:
        """EasyOCR-совместимый ``readtext``: detail=0 -> [str], detail=1 -> [(bbox, str, conf)]."""
        items = self.read_items(image)
        if detail == 0:
            return [item[1] for item in items]
        return items

    def read_items(self, image: ImageInput) -> List[DetailItem]:
        """Строки с рамками и «уверенностью» (в Windows OCR её нет — 0.9)."""
        if image is None:
            return []
        try:
            img = _prepare_image(_pil_from_input(image), self.upscale, self.max_width)
        except Exception as exc:
            self.last_error = exc
            log.exception("WinOCR: не удалось подготовить изображение")
            return []

        rows = self._recognize_rows(img)
        if not rows:
            return []
        return [([[int(x), int(y)] for x, y in box], text, 0.9) for box, text in rows]

    def _recognize_rows(self, img) -> List[Tuple[List[List[int]], str]]:
        """Строки (box, text) с деградацией между бэкендами.

        Пустой список — это валидный ответ («на кадре нет текста») и он НЕ
        запускает фоллбэк. ``None`` — оба бэкенда не смогли ответить.
        """
        for name, runner in self._backend_chain():
            try:
                rows = runner(img)
            except Exception as exc:  # noqa: BLE001 - деградация, а не падение
                self._on_backend_failure(name, exc)
                continue
            self._on_backend_success(name)
            return rows or []
        return []

    def _backend_chain(self):
        """"auto" пробует winrt, затем powershell; явный бэкенд — только себя."""
        chain = []
        now = time.monotonic()
        if self.backend in (BACKEND_AUTO, BACKEND_WINRT):
            if now >= self._cooldown_until[BACKEND_WINRT]:
                chain.append((BACKEND_WINRT, self._rows_with_winrt))
        if self.backend in (BACKEND_AUTO, BACKEND_POWERSHELL):
            if time.monotonic() >= self._cooldown_until[BACKEND_POWERSHELL]:
                chain.append((BACKEND_POWERSHELL, self._rows_with_powershell))
        return chain

    def _on_backend_success(self, name):
        self._active_backend = name
        self._failures[name] = 0
        self._reported_failure[name] = False
        # Ошибку основного пути сохраняем, пока работаем на запасном: иначе
        # деградация выглядит как «всё хорошо» и причину не найти.
        if name == BACKEND_WINRT or self.backend == BACKEND_POWERSHELL:
            self.last_error = None

    def _on_backend_failure(self, name, exc):
        self.last_error = exc
        self._failures[name] += 1
        log.debug("WinOCR: бэкенд %s не сработал (%s: %s)", name, type(exc).__name__, exc)

        if self._failures[name] < _FAILURE_THRESHOLD:
            return  # разовый сбой — следующий кадр снова пойдёт основным путём

        if not self._reported_failure[name]:
            self._reported_failure[name] = True
            log.warning(
                "WinOCR: бэкенд %s не отвечает (%d ошибок подряд, последняя: %s). "
                "Пауза %d с, затем повтор.",
                name,
                self._failures[name],
                exc,
                int(_BACKEND_COOLDOWN),
            )
        self._cooldown_until[name] = time.monotonic() + _BACKEND_COOLDOWN

    def warmup(self) -> str:
        """Заранее поднимает движок и возвращает имя рабочего бэкенда.

        Первый вызов Windows.Media.Ocr дороже остальных (загрузка движка и
        языковых данных), поэтому прогреваем его пустым кадром ещё до того,
        как из буфера обмена придёт первый кадр.
        """
        self._active_backend = None
        try:
            from PIL import Image

            self.read_text(Image.new("RGB", (320, 96), "white"))
        except Exception:
            log.exception("WinOCR: прогрев движка не удался")

        if self._active_backend is None:
            raise OcrUnavailableError(
                "Windows OCR не отвечает ни через winrt, ни через PowerShell "
                "(последняя ошибка: %s). Проверьте языковой пакет OCR: "
                "Параметры -> Время и язык -> Язык -> Добавить язык."
                % (self.last_error,)
            )
        return self._active_backend

    # ------------------------------------------------------------------
    # бэкенд winrt
    # ------------------------------------------------------------------

    def _ensure_engine(self):
        """Создаёт OcrEngine один раз (потокобезопасно)."""
        engine = self._engine
        if engine is not None:
            return engine
        with self._lock:
            if self._engine is None:
                self._engine = self._create_engine()
            return self._engine

    def _create_engine(self):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        engine = None
        for tag in self.languages:
            try:
                engine = OcrEngine.try_create_from_language(Language(tag))
            except Exception:
                engine = None
            if engine is not None:
                self._language_tag = tag
                break

        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is not None:
                try:
                    self._language_tag = str(engine.recognizer_language.language_tag)
                except Exception:
                    self._language_tag = None

        if engine is None:
            installed = ", ".join(self.available_recognizer_languages()) or "нет"
            raise OcrUnavailableError(
                "Windows OCR не установлен ни для одного из языков "
                f"{self.languages}. Доступно в системе: {installed}. "
                "Установите языковой пакет: Параметры → Время и язык → Язык."
            )

        log.info(
            "Windows OCR: язык %s, движок %s",
            self._language_tag or "профиля",
            "winrt",
        )
        self._engine = engine
        return engine

    def _read_with_winrt(self, img) -> str:
        return self._run_async(self._recognize_async(img))

    def _rows_with_winrt(self, img) -> List[Tuple[List[List[int]], str]]:
        return self._run_async(self._rows_async(img))

    async def _decode_bitmap(self, img):
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(_png_bytes(img))
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        return await decoder.get_software_bitmap_async()

    async def _recognize_async(self, img) -> str:
        engine = self._ensure_engine()
        bitmap = await self._decode_bitmap(img)
        result = await engine.recognize_async(bitmap)
        # Windows OCR уже разделяет строки символом \n — склеиваем в пробел,
        # как это делал OCREngine.extract_text() на EasyOCR.
        return " ".join(part.strip() for part in str(result.text).splitlines() if part.strip()).strip()

    async def _rows_async(self, img) -> List[Tuple[List[List[int]], str]]:
        engine = self._ensure_engine()
        bitmap = await self._decode_bitmap(img)
        result = await engine.recognize_async(bitmap)

        rows: List[Tuple[List[List[int]], str]] = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            try:
                rect = line.words[0].bounding_rect
                left, top = int(rect.x), int(rect.y)
                right, bottom = left, top
                for word in words:
                    r = word.bounding_rect
                    left = min(left, int(r.x))
                    top = min(top, int(r.y))
                    right = max(right, int(r.x + r.width))
                    bottom = max(bottom, int(r.y + r.height))
                box = [[left, top], [right, top], [right, bottom], [left, bottom]]
            except Exception:
                box = []
            rows.append((box, str(line.text)))
        return rows

    @staticmethod
    def _run_async(coro, timeout: float = _ASYNC_TIMEOUT):
        """Выполняет корутину winrt в текущем потоке или в отдельном.

        asyncio.run() падает, если в потоке уже крутится событийный цикл
        (например, OCR позовут из GUI-потока с интеграцией asyncio). В этом
        случае заводим отдельный поток: pywinrt требует инициализации COM-
        apartment в каждом потоке, где создаются WinRT-объекты.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        box: dict = {}

        def runner():
            _init_apartment()
            try:
                box["value"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001 - пробрасываем в caller
                box["error"] = exc
            finally:
                _uninit_apartment()

        thread = threading.Thread(target=runner, name="winocr-async", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Windows OCR не ответил за %.0f с" % timeout)
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # ------------------------------------------------------------------
    # бэкенд powershell
    # ------------------------------------------------------------------

    def _read_with_powershell(self, img) -> str:
        rows = self._rows_with_powershell(img)
        return " ".join(text for _box, text in rows).strip()

    def _rows_with_powershell(self, img) -> List[Tuple[List[List[int]], str]]:
        image_path = self._write_temp_png(img)
        try:
            payloads = _powershell_recognize(image_path, self._powershell_language())
        finally:
            try:
                os.remove(image_path)
            except OSError:
                pass

        rows: List[Tuple[List[List[int]], str]] = []
        for payload in payloads:
            text_value = payload.get("text") or ""
            if not str(text_value).strip():
                continue
            box = payload.get("box") or []
            rows.append(
                (
                    [[int(p[0]), int(p[1])] for p in box if isinstance(p, (list, tuple)) and len(p) >= 2],
                    str(text_value),
                )
            )
        return rows

    def _powershell_language(self) -> str:
        for tag in self.languages:
            if tag:
                return tag
        return "en-US"

    @staticmethod
    def _write_temp_png(img) -> str:
        handle, path = tempfile.mkstemp(prefix="gadget_ocr_", suffix=".png")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(_png_bytes(img))
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return path


# ======================================================================
# PowerShell 5.1 fallback (тот же Windows.Media.Ocr)
# ======================================================================
# WinRT-типы доступны только в Windows PowerShell 5.1 (.NET Framework), а не
# в pwsh 7 (.NET Core). Результат пишем в файл в UTF-8: перекодировки stdout
# в консольной кодировке — самый частый источник «кракозябр» в этом сценарии.

_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
# stderr пишем в UTF-8: иначе русские сообщения об ошибках приходят в Python
# в консольной кодировке и превращаются в мусор.
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$path = '__IMAGE_PATH__'
$out  = '__OUT_PATH__'
$lang = '__LANGUAGE__'

try {
    # Без этой сборки .NET Framework в PowerShell 5.1 нет класса
    # System.WindowsRuntimeSystemExtensions, которым ждут IAsyncOperation.
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]             | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]          | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]    | Out-Null

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    function Await($operation, $resultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
        $netTask = $asTask.Invoke($null, @($operation))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    $file   = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $language = New-Object Windows.Globalization.Language($lang)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) { throw "Windows OCR engine is not available for language $lang" }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    $lines = New-Object System.Collections.Generic.List[string]
    # В PowerShell 5.1 индексатор у WinRT-коллекций (Lines[0], Words[0]) не
    # работает — объект приходит как System.__ComObject. Поэтому обходим
    # только через foreach и не трогаем .Count у Words.
    foreach ($line in $result.Lines) {
        $left = 0.0; $top = 0.0; $right = 0.0; $bottom = 0.0
        $first = $true
        foreach ($word in $line.Words) {
            # Явные приведения к [double]: свойства Rect приходят как WinRT-числа.
            $r = $word.BoundingRect
            $x1 = [double]$r.X
            $y1 = [double]$r.Y
            $x2 = $x1 + [double]$r.Width
            $y2 = $y1 + [double]$r.Height
            if ($first) {
                $left = $x1; $top = $y1; $right = $x2; $bottom = $y2
                $first = $false
            } else {
                if ($x1 -lt $left)   { $left = $x1 }
                if ($y1 -lt $top)    { $top = $y1 }
                if ($x2 -gt $right)  { $right = $x2 }
                if ($y2 -gt $bottom) { $bottom = $y2 }
            }
        }

        $box = @()
        if (-not $first) {
            $box = @(
                @([int][math]::Round($left),   [int][math]::Round($top)),
                @([int][math]::Round($right),  [int][math]::Round($top)),
                @([int][math]::Round($right),  [int][math]::Round($bottom)),
                @([int][math]::Round($left),   [int][math]::Round($bottom))
            )
        }
        $payload = [ordered]@{ text = [string]$line.Text; box = $box }
        $lines.Add(($payload | ConvertTo-Json -Compress -Depth 5))
    }

    # Маркер успешного завершения: пустой файл без него означает, что скрипт
    # не смог отработать, а не «на картинке нет текста».
    $lines.Add('{"ok":true}')

    $encoder = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($out, ($lines -join "`n"), $encoder)
} catch {
    # Ошибку тоже пишем в UTF-8-файл: stdout в консольной кодировке
    # превращает русский текст в мусор при чтении из Python.
    try {
        $encoder = New-Object System.Text.UTF8Encoding($false)
        $errPayload = [ordered]@{ error = [string]$_.Exception.Message }
        [System.IO.File]::WriteAllText($out, ($errPayload | ConvertTo-Json -Compress), $encoder)
    } catch { }
    [Console]::Error.WriteLine(($_.Exception.Message))
    exit 3
}
"""


def _powershell_executable() -> Optional[str]:
    """Путь к Windows PowerShell 5.1 (WinRT-типы есть только в .NET Framework)."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.exists(candidate):
        return candidate
    from shutil import which

    return which("powershell.exe")


def _ps_quote(value: str) -> str:
    """Экранирует строку для вставки в одинарные кавычки PowerShell."""
    return str(value).replace("'", "''")


def _run_powershell(script: str, timeout: int = 30):
    executable = _powershell_executable()
    if not executable:
        raise OcrUnavailableError("powershell.exe не найден")

    handle, script_path = tempfile.mkstemp(prefix="gadget_ocr_", suffix=".ps1")
    try:
        # Windows PowerShell 5.1 читает .ps1 в UTF-8 только при наличии BOM.
        with os.fdopen(handle, "w", encoding="utf-8-sig") as stream:
            stream.write(script)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", script_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def _powershell_recognize(image_path: str, language: str) -> List[dict]:
    """Запускает PS-скрипт и читает JSON-строки результата из файла."""
    import json

    out_handle, out_path = tempfile.mkstemp(prefix="gadget_ocr_", suffix=".jsonl")
    os.close(out_handle)
    try:
        script = (
            _POWERSHELL_SCRIPT
            .replace("__IMAGE_PATH__", _ps_quote(os.path.abspath(image_path)))
            .replace("__OUT_PATH__", _ps_quote(os.path.abspath(out_path)))
            .replace("__LANGUAGE__", _ps_quote(language))
        )
        result = _run_powershell(script)

        try:
            with open(out_path, "r", encoding="utf-8-sig") as stream:
                raw = stream.read()
        except OSError:
            raw = ""

        payloads: List[dict] = []
        finished = False
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("error"):
                raise OcrUnavailableError("PowerShell OCR: %s" % item["error"])
            if item.get("ok"):
                finished = True
                continue
            payloads.append(item)

        if result.returncode != 0:
            raise OcrUnavailableError(
                "PowerShell OCR завершился с кодом %s: %s"
                % (result.returncode, (result.stderr or "").strip()[:400])
            )
        if not finished:
            raise OcrUnavailableError(
                "PowerShell OCR не вернул результат (пустой ответ). stderr: %s"
                % ((result.stderr or "").strip()[:400] or "нет")
            )

        return payloads
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def _powershell_ocr_available() -> bool:
    """Быстрая проверка: есть ли powershell.exe и видит ли он WinRT OCR."""
    try:
        executable = _powershell_executable()
        if not executable:
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = subprocess.run(
            [
                executable, "-NoProfile", "-NonInteractive", "-Command",
                "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null; "
                "if ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages.Count -gt 0) { exit 0 } else { exit 1 }",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
        )
        return result.returncode == 0
    except Exception:
        return False

