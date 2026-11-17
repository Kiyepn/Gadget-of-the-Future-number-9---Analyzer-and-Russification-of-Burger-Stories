@echo off
setlocal
chcp 65001 >nul

REM ============================================================
REM  Сборка GadgetOfTheFuture в Windows .exe (режим --onedir)
REM  Библиотеки: easyocr, torch, torchvision, PIL, numpy, requests, pynput
REM
REM  ВАЖНО: сборка выполняется БЕЗ --uac-admin (asInvoker). Раньше
REM  приложение запускалось с правами администратора, и Windows UIPI
REM  (User Interface Privilege Isolation) не давала процессу с высоким
REM  уровнем целостности рисовать оверлей поверх обычных окон (игр,
REM  браузеров). Из-за этого оверлей был невидим. Сборка обязана
REM  оставаться asInvoker — без каких-либо запросов прав администратора.
REM ============================================================

cd /d "%~dp0"

echo [1/5] Устанавливаю PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [Ошибка] Не удалось установить PyInstaller.
    pause
    exit /b 1
)

echo [2/5] Закрываю запущенные копии и очищаю старые артефакты (build/, dist/, *.spec)...
REM Если приложение сейчас запущено, Windows держит файлы dist\ и сборка
REM получится битой. Поэтому сначала закрываем все копии.
taskkill /IM GadgetOfTheFuture.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist dist (
    echo.
    echo [Ошибка] Папку dist не удалось удалить: она занята запущенным процессом.
    echo Закрой GadgetOfTheFuture.exe (и лаунчер) и запусти сборку заново.
    pause
    exit /b 1
)
if exist "*.spec" del /q "*.spec"

echo [3/5] Запускаю PyInstaller (onedir + windowed, БЕЗ прав администратора)...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --windowed ^
    --name GadgetOfTheFuture ^
    --collect-all customtkinter ^
    --collect-all easyocr ^
    --collect-all torch ^
    --collect-all torchvision ^
    --collect-all pynput ^
    --hidden-import main ^
    gui_launcher.py
if errorlevel 1 (
    echo.
    echo [Ошибка] Сборка завершилась с ошибкой. Смотри вывод выше.
    pause
    exit /b 1
)

echo [4/5] Копирую config.py и создаю пустую папку data\ рядом с .exe...
copy /y config.py "dist\GadgetOfTheFuture\config.py" >nul
if not exist "dist\GadgetOfTheFuture\data" mkdir "dist\GadgetOfTheFuture\data"
type nul > "dist\GadgetOfTheFuture\data\.gitkeep"
REM Персональные данные разработчика НЕ копируем (в них API-ключ и личное):
REM   data\memory.json, data\settings.json, data\characters.json,
REM   data\glossary.json, data\web_cache.json, data\gadget.log

echo [5/5] Упаковываю релиз в ZIP...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\GadgetOfTheFuture\*' -DestinationPath 'dist\GadgetOfTheFuture-v1.4.3.zip' -CompressionLevel Optimal -Force"
if errorlevel 1 (
    echo [Ошибка] Не удалось упаковать ZIP.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Готово! Исполняемый файл:
echo    dist\GadgetOfTheFuture\GadgetOfTheFuture.exe
echo.
echo  Архив релиза:
echo    dist\GadgetOfTheFuture-v1.4.3.zip
echo.
echo  Рядом с exe уже лежат:
echo    dist\GadgetOfTheFuture\config.py
echo    dist\GadgetOfTheFuture\data\  (пустая, только .gitkeep)
echo.
echo  Размер архива:
dir "dist\GadgetOfTheFuture-v1.4.3.zip"
echo.
echo  ВАЖНО: при первом запуске EasyOCR скачает свои модели
echo  (обычно в %%USERPROFILE%%\.EasyOCR\model).
echo.
echo  Дальнейшие шаги:
echo    1. Проверь, что переводчик запускается БЕЗ прав администратора.
echo    2. Загрузи dist\GadgetOfTheFuture-v1.4.3.zip в релиз v1.4.3.
echo    3. Убедись, что в data\ нет личных файлов (memory/settings/ключ).
echo ============================================================
echo.
pause
endlocal
