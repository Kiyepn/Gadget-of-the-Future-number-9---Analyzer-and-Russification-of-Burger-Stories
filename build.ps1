# build.ps1
# Сборка «Гаджет Будущего» v1.5.0-beta (Lightweight) в .exe (--onedir).
#
# Запускается через build.bat (ASCII-обёртка). Логика внутри PowerShell, а не
# cmd, по важной причине: cmd.exe разбирает .bat в текущей кодовой странице
# консоли, и строка "chcp" внутри bat-файла сбивает смещение повторного чтения
# файла. На файлах больше ~4.5 КБ это ломает многострочную команду PyInstaller
# (симптом: "'--name' is not recognized as an internal or external command").
#
# Что изменилось по сравнению с v1.4.3:
#   * OCR теперь Windows Native (Windows.Media.Ocr) вместо EasyOCR, поэтому в
#     сборку НЕ входят easyocr, torch и torchvision;
#   * вместо --collect-all easyocr/torch/torchvision — --collect-all winrt;
#   * тяжёлые модули явно исключены (--exclude-module): brain/ocr_engine.py
#     остался в исходниках, и без исключений PyInstaller подтянул бы их снова;
#   * ZIP кладётся в dist\ рядом с папкой сборки, чистится только то, что
#     создаёт эта сборка (прошлые архивы не удаляются).
#
# ВАЖНО: сборка выполняется БЕЗ --uac-admin (asInvoker). Раньше приложение
# запускалось с правами администратора, и Windows UIPI (User Interface
# Privilege Isolation) не давала процессу с высоким уровнем целостности
# рисовать оверлей поверх обычных окон (игр, браузеров). Из-за этого оверлей
# был невидим. Сборка обязана оставаться asInvoker.

# $ErrorActionPreference = "Stop" здесь намеренно НЕ ставится: в Windows
# PowerShell 5.1 любая запись в stderr от внешней программы (pip, PyInstaller,
# taskkill) при Stop превращается в terminating error и роняет скрипт. Вместо
# этого у каждого cmdlet'а, где нужна строгость, указан -ErrorAction Stop, а
# результат внешних программ проверяется по $LASTEXITCODE.
$ErrorActionPreference = "Continue"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # В некоторых окружениях (нет консоли) переключение кодировки недоступно.
}

$AppName = "GadgetOfTheFuture"
$Version = "v1.5.0-beta"
$SizeLimitMb = 100

function Stop-WithError([string]$Message) {
    Write-Host ""
    Write-Host "[Ошибка] $Message" -ForegroundColor Red
    Write-Host ""
    if ($Host.Name -eq "ConsoleHost" -and [Environment]::UserInteractive) {
        try { $null = Read-Host "Нажмите Enter, чтобы закрыть окно" } catch { }
    }
    exit 1
}

# --- Проверка корня проекта --------------------------------------------------
# Защита от запуска скрипта через iex/из чужого каталога: без этой проверки
# $Root мог оказаться пустым и Remove-Item -Recurse снёс бы build/dist в
# текущем каталоге.
$Root = $null
try {
    if ($MyInvocation.MyCommand.Path) {
        $Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
    }
} catch {
    $Root = $null
}
if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) {
    Stop-WithError "Не удалось определить каталог проекта (запускайте через build.bat)."
}
if (-not (Test-Path -LiteralPath (Join-Path $Root "gui_launcher.py") -PathType Leaf)) {
    Stop-WithError "В каталоге $Root нет gui_launcher.py — это не корень проекта."
}
Set-Location -LiteralPath $Root

$DistRoot = Join-Path $Root "dist"
$DistDir = Join-Path $DistRoot $AppName
$ZipPath = Join-Path $DistRoot "$AppName-$Version.zip"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Green
}

Write-Host "============================================================" -ForegroundColor DarkGray
Write-Host " Сборка $AppName $Version (Lightweight, Windows Native OCR)" -ForegroundColor White
Write-Host " Проект: $Root" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor DarkGray

# --- [1/5] PyInstaller -------------------------------------------------------
Write-Step "[1/5] Устанавливаю PyInstaller..."
& python -m pip install --upgrade --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Не удалось установить PyInstaller."
}
Write-Ok "PyInstaller готов."

# --- [2/5] Предполётная проверка зависимостей --------------------------------
Write-Step "[2/5] Проверяю, что зависимости для Windows OCR на месте..."
& python -c "import PIL, customtkinter, winrt.windows.media.ocr, winrt.windows.graphics.imaging, winrt.windows.storage.streams, winrt.windows.globalization; print('deps ok')"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Не все зависимости установлены. Выполните: python -m pip install -r requirements.txt"
}
Write-Ok "Зависимости на месте (winrt + Pillow + CustomTkinter)."

# --- [3/5] Очистка -----------------------------------------------------------
Write-Step "[3/5] Закрываю запущенные копии и очищаю артефакты прошлой сборки..."

# Если приложение сейчас запущено, Windows держит файлы dist\ и сборка
# получится битой. Поэтому сначала закрываем все копии и ждём освобождения.
Get-Process -Name $AppName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
try { Wait-Process -Name $AppName -Timeout 10 -ErrorAction SilentlyContinue } catch { }

# Удаляем только то, что создаёт эта сборка. Раньше здесь сносился весь dist\,
# то есть вместе с прошлыми релизными архивами.
foreach ($path in @((Join-Path $Root "build"), $DistDir)) {
    if (Test-Path -LiteralPath $path) {
        $removed = $false
        for ($attempt = 1; $attempt -le 5 -and -not $removed; $attempt++) {
            try {
                Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
                $removed = $true
            } catch {
                Start-Sleep -Milliseconds 400
            }
        }
        if (-not $removed) {
            Stop-WithError "Не удалось удалить $path (папка занята запущенным процессом). Закрой $AppName.exe и повтори сборку."
        }
    }
}

# Спека PyInstaller теперь пишется в build\, а не в корень. Старый мусорной
# GadgetOfTheFuture.spec из корня (оставшийся от прошлых сборок) удаляем, чтобы
# никто не пытался собирать по устаревшему файлу.
$specPath = Join-Path $Root "$AppName.spec"
if (Test-Path -LiteralPath $specPath) {
    Remove-Item -LiteralPath $specPath -Force -ErrorAction SilentlyContinue
}
$buildDir = Join-Path $Root "build"
if (-not (Test-Path -LiteralPath $buildDir)) {
    New-Item -ItemType Directory -Path $buildDir | Out-Null
}
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
}
Write-Ok "Артефакты прошлой сборки удалены (build\, dist\$AppName, старый ZIP)."

# --- [4/5] PyInstaller -------------------------------------------------------
Write-Step "[4/5] Запускаю PyInstaller (onedir + windowed, БЕЗ прав администратора)..."

$pyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onedir"
    "--windowed"
    "--name", $AppName

    # .spec-файл генерируется заново при каждой сборке. Держим его в build\,
    # чтобы он не появлялся в корне проекта и не путал (в git его нет — *.spec
    # в .gitignore, источник истины — этот скрипт).
    "--specpath", (Join-Path $Root "build")

    # Только то, что реально нужно приложению.
    "--collect-all", "customtkinter"
    "--collect-all", "winrt"
    "--collect-all", "pynput"

    "--hidden-import", "main"
    "--hidden-import", "winrt.windows.media.ocr"
    "--hidden-import", "winrt.windows.graphics.imaging"
    "--hidden-import", "winrt.windows.storage.streams"
    "--hidden-import", "winrt.windows.globalization"

    # Исключения: без них PyInstaller подтянет torch/easyocr через
    # brain/ocr_engine.py (он остался в исходниках) и сборка снова потяжелеет
    # до ~900 МБ.
    "--exclude-module", "easyocr"
    "--exclude-module", "torch"
    "--exclude-module", "torchvision"
    "--exclude-module", "torchaudio"
    "--exclude-module", "cv2"
    "--exclude-module", "scipy"
    "--exclude-module", "numpy"
    "--exclude-module", "skimage"
    "--exclude-module", "matplotlib"
    "--exclude-module", "pandas"

    "gui_launcher.py"
)

& python -m PyInstaller @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Сборка завершилась с ошибкой. Смотри вывод выше."
}
Write-Ok "PyInstaller отработал."

# --- Проверка бандла ---------------------------------------------------------
Write-Step "Проверяю, что в сборку попал winrt и не попал torch..."
$winrtDir = Join-Path $DistDir "_internal\winrt"
if (-not (Test-Path -LiteralPath $winrtDir -PathType Container)) {
    Stop-WithError "В сборке нет $winrtDir — Windows OCR не заработает в exe."
}
$winrtOcr = Join-Path $winrtDir "windows\media\ocr\__init__.py"
if (-not (Test-Path -LiteralPath $winrtOcr)) {
    Stop-WithError "В сборке нет winrt.windows.media.ocr — проверь --collect-all winrt."
}
foreach ($heavy in @("torch", "easyocr", "torchvision")) {
    if (Test-Path -LiteralPath (Join-Path $DistDir "_internal\$heavy") -PathType Container) {
        Stop-WithError "В сборку попал $heavy — проверь --exclude-module."
    }
}
Write-Ok "winrt на месте, torch/easyocr отсутствуют."

# --- Файлы рядом с exe -------------------------------------------------------
Write-Step "Копирую config.py, README для пользователя и создаю пустую папку data\..."
Copy-Item -LiteralPath (Join-Path $Root "config.py") -Destination (Join-Path $DistDir "config.py") -Force -ErrorAction Stop
$userReadme = Join-Path $Root "packaging\README-USER.txt"
if (Test-Path -LiteralPath $userReadme -PathType Leaf) {
    Copy-Item -LiteralPath $userReadme -Destination (Join-Path $DistDir "README.txt") -Force -ErrorAction Stop
} else {
    Write-Warning "Нет packaging\README-USER.txt — архив уйдёт без инструкции."
}
$dataDir = Join-Path $DistDir "data"
if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}
Set-Content -LiteralPath (Join-Path $dataDir ".gitkeep") -Value "" -NoNewline

# Персональные данные разработчика НЕ копируем (в них API-ключ и личное):
#   data\memory.json, data\settings.json, data\characters.json,
#   data\glossary.json, data\web_cache.json, data\gadget.log
Write-Ok "config.py и README.txt на месте, data\ пустая."

# --- [5/5] ZIP ---------------------------------------------------------------
# Архивируем саму папку, а не её содержимое: при распаковке у пользователя
# появляется одна папка GadgetOfTheFuture, а не 1000+ файлов в текущем каталоге.
Write-Step "[5/5] Упаковываю релиз в ZIP..."
try {
    Compress-Archive -Path $DistDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force -ErrorAction Stop
} catch {
    Stop-WithError "Не удалось упаковать ZIP: $($_.Exception.Message)"
}
Write-Ok "ZIP готов."

# --- Контрольная сумма -------------------------------------------------------
# Файл .sha256 рядом с архивом: пользователь может сверить хэш перед запуском
# (полезно, пока exe не подписан сертификатом).
$shaPath = "$ZipPath.sha256"
try {
    $hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLower()
    Set-Content -LiteralPath $shaPath -Value ("{0}  {1}" -f $hash, (Split-Path -Leaf $ZipPath)) -Encoding ascii
} catch {
    $hash = $null
    Write-Warning "Не удалось посчитать SHA256: $($_.Exception.Message)"
}

# --- Итоги -------------------------------------------------------------------
$folderBytes = (Get-ChildItem -LiteralPath $DistDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
$folderMb = [math]::Round($folderBytes / 1MB, 1)
$zipMb = [math]::Round((Get-Item -LiteralPath $ZipPath -ErrorAction Stop).Length / 1MB, 2)

Write-Host ""
Write-Host "============================================================" -ForegroundColor DarkGray
Write-Host " Готово!" -ForegroundColor Green
Write-Host "   exe:  dist\$AppName\$AppName.exe"
Write-Host "   ZIP:  dist\$AppName-$Version.zip"
Write-Host ""
if ($hash) {
    Write-Host "   SHA256: $hash" -ForegroundColor DarkGray
    Write-Host "   (он же в файле $AppName-$Version.zip.sha256)" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "   Размер папки: $folderMb МБ"
Write-Host "   Размер ZIP:   $zipMb МБ"
if ($zipMb -lt $SizeLimitMb) {
    Write-Host "   Цель (< $SizeLimitMb МБ) достигнута." -ForegroundColor Green
} else {
    Write-Host "   ВНИМАНИЕ: ZIP больше $SizeLimitMb МБ - проверь исключения PyInstaller." -ForegroundColor Yellow
}
Write-Host ""
Write-Host " Рядом с exe уже лежат:" -ForegroundColor Gray
Write-Host "   dist\$AppName\config.py"
Write-Host "   dist\$AppName\data\  (пустая, только .gitkeep)"
Write-Host ""
Write-Host " ВАЖНО: для OCR нужен языковой пакет Windows (en-US)." -ForegroundColor Gray
Write-Host " Проверка: Параметры -> Время и язык -> Язык -> Добавить язык."
Write-Host " Смоук-тест: запусти exe и убедись, что в data\gadget.log есть строка"
Write-Host ' "Используется OCR: Windows Native".' -ForegroundColor Gray
Write-Host ""
Write-Host " Дальнейшие шаги:" -ForegroundColor Gray
Write-Host "   1. Проверь, что переводчик запускается БЕЗ прав администратора."
Write-Host "   2. Убедись, что ZIP меньше $SizeLimitMb МБ и оверлей работает."
Write-Host "   3. Публиковать релиз на GitHub пока НЕ нужно (локальная beta)."
Write-Host "============================================================" -ForegroundColor DarkGray

if ($Host.Name -eq "ConsoleHost" -and [Environment]::UserInteractive) {
    Write-Host ""
    try { $null = Read-Host "Нажмите Enter, чтобы закрыть окно" } catch { }
}

exit 0
