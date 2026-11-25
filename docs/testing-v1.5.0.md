# Как тестировать v1.5.0 (Lightweight)

Документ для локальной проверки сборки: что запускать, что должно получиться и
что делать, если не получилось. Всё, кроме отмеченного «вручную», проверяется
командами.

Окружение проверено на: Windows 11 (build 26300), Python 3.14.6, PyInstaller 6.22.3.

---

## 0. Что должно быть на машине

| Что | Как проверить | Зачем |
| --- | --- | --- |
| Windows 10/11 x64 | `winver` | Windows OCR есть только здесь |
| Языковой пакет OCR `en-US` | `python -c "from brain.ocr_providers.win_ocr import WinOCR; print(WinOCR.available_recognizer_languages())"` → должен содержать `en-US` | без него Windows OCR не работает |
| Python 3.14 + deps | `python -m pip install -r requirements.txt` | для запуска из исходников |
| Ollama (или ключ DeepSeek) | `ollama list` | для перевода; для тестов OCR не нужен |

---

## 1. Автотесты (2 минуты)

```powershell
cd C:\Users\Mr Kiyepon\Desktop\GadgetOfTheFuture
python -m unittest discover -s tests -v
```

Ожидаемо: `Ran 43 tests ... OK`.

- 13 юнит-тестов `test_win_ocr` работают **всегда** (логика без движка).
- 6 «живых» тестов пропускаются (`skipped`), если в системе нет языкового пакета
  OCR — это нормально, но на твоей машине они должны проходить.
- `test_overlay` открывает реальные окна и снимает экран — если тесты идут на
  машине без интерактивного рабочего стола (RDP-сессия, CI), они упадут. Это
  ограничение тестов, а не приложения.

Только OCR-тесты:

```powershell
python -m unittest tests.test_win_ocr -v
```

---

## 2. Сборка (2 минуты)

```powershell
cd C:\Users\Mr Kiyepon\Desktop\GadgetOfTheFuture
build.bat
```

Ожидаемо в консоли:

```
==> [2/5] Проверяю, что зависимости для Windows OCR на месте...
    deps ok
    winrt на месте, torch/easyocr отсутствуют.
    Размер папки: ~45 МБ
    Размер ZIP:   ~22 МБ
    Цель (< 100 МБ) достигнута.
```

Что проверить после сборки:

| Проверка | Команда | Ожидаемо |
| --- | --- | --- |
| ZIP меньше 100 МБ | `Get-ChildItem dist\*.zip \| Select Length` | ~22 МБ |
| В корне проекта нет `.spec` | `Get-ChildItem *.spec` | пусто (спека теперь в `build\`) |
| В сборке есть winrt | `Test-Path dist\GadgetOfTheFuture\_internal\winrt\windows\media\ocr\__init__.py` | `True` |
| В сборке нет torch/easyocr/numpy | `Get-ChildItem dist\GadgetOfTheFuture\_internal -Directory \| ? Name -match 'torch\|easyocr\|numpy'` | пусто |
| В ZIP одна папка | `Add-Type -A System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::OpenRead("$PWD\dist\GadgetOfTheFuture-v1.5.0.zip").Entries[0].FullName` | `GadgetOfTheFuture\...` |
| Рядом с exe есть README.txt | `Test-Path dist\GadgetOfTheFuture\README.txt` | `True` |
| Хэш совпадает | `Get-FileHash dist\*.zip -Algorithm SHA256` и `Get-Content dist\*.zip.sha256` | одинаковые хэши |

Отрицательный сценарий сборки: запусти сборку второй раз при запущенном exe — она
должна закрыть процесс, дождаться и собраться, а если папка держится — сказать
«Не удалось удалить ... Закрой GadgetOfTheFuture.exe».

---

## 3. Смоук-тест OCR без GUI (30 секунд)

```powershell
python -c @"
from PIL import Image, ImageDraw, ImageFont
from brain.ocr_providers.win_ocr import WinOCR
img = Image.new('RGB', (700, 200), (18,18,24))
d = ImageDraw.Draw(img)
d.text((20, 60), 'Ryoshu: the city never sleeps.', fill='white', font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 20))
ocr = WinOCR()
print('backend:', ocr.warmup(), 'lang:', ocr.language_tag)
print('text:', ocr.read_text(img))
"@
```

Ожидаемо: `backend: winrt lang: en-US` и `text: Ryoshu: the city never sleeps.`
(10–40 мс). Если вывод пустой — смотри `available_recognizer_languages()`.

Так же проверяется запасной PowerShell-бэкенд:

```powershell
python -c "from PIL import Image; from brain.ocr_providers.win_ocr import WinOCR; print(WinOCR(backend='powershell').read_text(Image.new('RGB',(64,64),'white')) or 'ok (пусто = текста нет)')"
```

---

## 4. Смоук собранного exe (2 минуты, вручную частично)

### 4.1 Оверлей

```powershell
dist\GadgetOfTheFuture\GadgetOfTheFuture.exe --overlay-test --seconds 5
```

Ожидаемо: на 5 секунд появляется тёмная карточка с английским текстом и русским
переводом внизу экрана; клики проходят сквозь неё; окна в панели задач нет.

### 4.2 OCR в замороженном exe

1. Сделай скриншот с английским текстом: `Win+Shift+S` (или открой любую VN).
2. Запусти:

```powershell
Remove-Item dist\GadgetOfTheFuture\data\gadget.log -ErrorAction SilentlyContinue
Start-Process dist\GadgetOfTheFuture\GadgetOfTheFuture.exe -ArgumentList "--no-overlay"
Start-Sleep 8
Stop-Process -Name GadgetOfTheFuture -Force
Get-Content dist\GadgetOfTheFuture\data\gadget.log
```

Ожидаемо в журнале:

```
Windows OCR: язык en-US, движок winrt
Windows Native OCR прогрелся (бэкенд winrt).
Используется OCR: Windows Native
OCR готов за 0.0X с
OCR: <текст со скриншота>
```

Если Ollama запущена — дальше появится `Перевод: ...`. Если нет — ошибка перевода,
но OCR уже проверен.

### 4.3 Отрицательный сценарий: easyocr недоступен

```powershell
$env:GADGET_OCR_PROVIDER = "easyocr"
Start-Process dist\GadgetOfTheFuture\GadgetOfTheFuture.exe -ArgumentList "--no-overlay"
Start-Sleep 6
Stop-Process -Name GadgetOfTheFuture -Force
Remove-Item Env:GADGET_OCR_PROVIDER
Get-Content dist\GadgetOfTheFuture\data\gadget.log -Tail 6
```

Ожидаемо: в журнале `Не удалось запустить переводчик` и сообщение вида
«модуль easyocr не импортируется (в сборке Lightweight нет easyocr и torch)…»,
а не трейсбек `ModuleNotFoundError`. Приложение выходит с кодом 1, а не зависает.

### 4.4 Повторный запуск

Запусти exe дважды: второй экземпляр должен в журнале написать
«Приложение уже запущено — выходим» и сразу закрыться.

---

## 5. Функциональный чеклист (вручную, 10 минут)

| # | Проверка | Ожидаемо |
| --- | --- | --- |
| 1 | `Win+Shift+S` → выделить текст в игре → `Ctrl+C`-эквивалент (скриншот уже в буфере) | в оверлее появляется перевод через 1–3 с |
| 2 | `F6` | последний перевод показан снова |
| 3 | `F7` | карточка скрылась/показалась |
| 4 | `F8` | в журнале «Пауза: вкл», новые кадры не переводятся; `F8` снова — «выкл» |
| 5 | `F9` | «Память диалога очищена», `data\memory.json` пустой |
| 6 | Смена модели на «DeepSeek (облако)» + ключ | перевод идёт через облако, ключ в `settings.json` в виде `dpapi:...` |
| 7 | Запуск лаунчера без прав администратора | в журнале `Права администратора: False` |
| 8 | Запуск лаунчера от администратора | предупреждение UIPI в журнале/карточке |
| 9 | Игра в режиме «оконный без рамки» | карточка видна поверх |
| 10 | Игра в эксклюзивном полноэкранном | карточка не видна — это ограничение Windows, не баг |
| 11 | Смена позиции/прозрачности/размера шрифта в лаунчере | применяется сразу |
| 12 | Перенос папки на другой диск и запуск | работает, данные пишутся в `data\` рядом с exe |
| 13 | Запуск из папки только для чтения (например, `C:\Program Files`) | данные уходят в `%LOCALAPPDATA%\GadgetOfTheFuture\data`, путь виден в лаунчере |

---

## 6. Проверка «как обычный пользователь» (самое важное)

### 6.1 Проверка «с нуля» на чистом Python (для тех, кто ставит из исходников)

```powershell
python -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-clean\Scripts\python.exe -m unittest discover -s tests
.\.venv-clean\Scripts\python.exe -c "import importlib.util as u; print('torch:', u.find_spec('torch') is not None)"
```

Ожидаемо: установились 14 пакетов (21 колесо с зависимостями), 43 теста OK,
`torch: False`. Проверено на этой машине — так выглядит путь обычного
пользователя, который клонировал репозиторий.

### 6.2 Проверка переносимости архива (эмуляция «скачал чужой человек»)

```powershell
$tmp = Join-Path $env:TEMP "gadget_user_test"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Expand-Archive dist\GadgetOfTheFuture-v1.5.0.zip -DestinationPath $tmp
Get-ChildItem $tmp | Select-Object Name          # должна быть одна папка
Start-Process (Join-Path $tmp "GadgetOfTheFuture\GadgetOfTheFuture.exe")
Start-Sleep 8
Stop-Process -Name GadgetOfTheFuture -Force
Get-Content (Join-Path $tmp "GadgetOfTheFuture\data\gadget.log") -Tail 8
```

Ожидаемо: приложение запускается из любой папки (не только из репозитория) и
пишет в свой `data\gadget.log` строку `Используется OCR: Windows Native`.
Если запустить exe прямо из окна ZIP-просмотрщика, Windows распакует его во
временную папку только для чтения — данные не сохранятся. Поэтому в `README.txt`
первым шагом стоит «распакуй целиком».

### 6.3 Что видит обычный пользователь при скачивании (проверка глазами)

1. Скачай `GadgetOfTheFuture-v1.5.0.zip` (22 МБ) как обычный файл.
2. Распакуй (правый клик → «Извлечь всё»). Должна появиться **одна** папка
   `GadgetOfTheFuture` с `GadgetOfTheFuture.exe`, `README.txt`, `config.py`,
   `data\` и `_internal\`.
3. Запусти `GadgetOfTheFuture.exe` **без** прав администратора.
4. Что проверить:
   - Windows SmartScreen покажет «Windows защитила ваш компьютер» — это
     **нормально** для неподписанного exe. Пользователь: «Подробнее» → «Выполнить».
   - Антивирус может ругаться на PyInstaller-сборку (эвристика). Проверь на
     VirusTotal, если планируешь публиковать.
   - Без Ollama/ключа лаунчер запустится, но перевод не пойдёт — это ожидаемо и
     описано в `README.txt`.
   - Быстрая проверка OCR без перевода: `--overlay-test` или см. п. 4.2.

Известные шероховатости обычного пользователя (важно понимать):

| Шероховатость | Насколько критично | Что можно сделать |
| --- | --- | --- |
| SmartScreen-предупреждение | Средне | код-сигнинг сертификат (платно) или инструкция + хэш SHA256 |
| Нет языкового пакета `en-US` | Высоко: OCR не работает | подсказка в лаунчере со ссылкой `ms-settings:regionlanguage` (v1.5.1) |
| Нужен Ollama / ключ DeepSeek | Высоко для «просто скачал» | уже описано в `README.txt`; можно добавить мастер первого запуска |
| Нет установщика, портативная папка | Низко | ок для portable; при желании — Inno Setup |
| Запуск из ZIP без распаковки | Средне | в `README.txt` явно сказано «распакуй целиком» |
| Русские имена папок/пробелы в пути | Низко | проверено: путь с пробелом работает |

---

### 6.4 Проверка лаунчера v1.5.1 (Ollama, темы, ярлык, загрузка)

| # | Что | Как проверить | Ожидаемо |
| --- | --- | --- | --- |
| 1 | Баннер «Ollama не установлена» | Остановить сервис: `Get-Process ollama* \| Stop-Process -Force`, открыть лаунчер | Вверху баннер «Ollama не установлена. Скачайте для локального перевода.» + кнопки «Скачать Ollama» / «Проверить снова» |
| 2 | Баннер при установленной, но остановленной Ollama | То же, но CLI остаётся установленным | Баннер: «Ollama установлена, но сервис не запущен» |
| 3 | Кнопка «Скачать Ollama» | Нажать | Открывается https://ollama.com/download/windows в браузере |
| 4 | Проверка после запуска | Запустить Ollama, нажать «Проверить снова» | Баннер исчезает |
| 5 | Запуск без Ollama | Нажать «Запустить переводчик» при баннере | Переводчику не даёт стартовать, внизу понятное сообщение |
| 6 | Прогресс загрузки | Выбрать в списке модель, которой нет | Появляется карточка «Загрузка модели»: полоска, «ГБ / ГБ · % · скорость», строка «осталось … · ~время», консоль со стадиями |
| 7 | Отмена загрузки | Нажать «Отмена» | Загрузка останавливается, статус «Загрузка отменена» |
| 8 | Темы | Переключить 6 тем в «Оформление лаунчера» | Окно перекрашивается сразу, выбор сохраняется после перезапуска; карточка перевода использует тот же акцент (проверить после перезапуска переводчика) |
| 9 | Ярлык | Нажать «Создать ярлык сейчас» | На рабочем столе появляется «Гаджет Будущего.lnk», запускает лаунчер |
| 10 | Автоярлык | Удалить ярлык, запустить собранный exe | Ярлык создаётся сам (если включён переключатель) |
| 11 | Смена темы при работающем переводчике | Запустить переводчик, сменить тему | Окно перекрашивается, кнопка «Остановить» активна и останавливает переводчик |
| 12 | Цвет оверлея | Выбрать «Оранжевая», нажать «Проверить оверлей» | Тестовая карточка и статусы («Готово…», «Продолжаю») в оранжевом цвете, без кружочка в шапке |

Важно: п. 1–2 требуют остановки Ollama. Если Ollama нужна другим программам —
проверяй в конце, потом запусти её обратно (`ollama serve` или из меню «Пуск»).

## 7. Критерии приёмки релиза
Релиз v1.5.0 можно публиковать, если:

- [ ] `python -m unittest discover -s tests` → 43 теста, OK
- [ ] `build.bat` завершается без ошибок, в логе «Цель (< 100 МБ) достигнута»
- [ ] ZIP ≤ 100 МБ и содержит одну корневую папку + `README.txt`
- [ ] в журнале собранного exe есть `Используется OCR: Windows Native`
- [ ] выбранный `easyocr` даёт понятную ошибку, а не трейсбек
- [ ] чеклист п. 5 пройден без падений (кроме п. 10 — это ограничение Windows)
- [ ] собранный exe проверен на VirusTotal (нет новых детекций)
- [ ] в репозитории нет личных данных (`data\memory.json`, `settings.json`,
      `gadget.log`) и они не попали в ZIP

---

## 8. Если что-то упало: что прислать

1. `data\gadget.log` (или `dist\GadgetOfTheFuture\data\gadget.log`).
2. Вывод сборки целиком (консоль `build.bat`).
3. `python -c "import sys; print(sys.version)"`, версия Windows (`winver`).
4. `python -c "from brain.ocr_providers.win_ocr import WinOCR; print(WinOCR.available_recognizer_languages(), WinOCR.is_available())"`.
