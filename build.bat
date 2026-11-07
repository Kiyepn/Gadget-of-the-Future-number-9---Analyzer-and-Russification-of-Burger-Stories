@echo off
setlocal
chcp 65001 >nul

REM ============================================================
REM  Сборка GadgetOfTheFuture в Windows .exe (режим --onedir)
REM  Библиотеки: easyocr, torch, torchvision, PIL, numpy, requests, keyboard
REM ============================================================

cd /d "%~dp0"

echo [1/4] Устанавливаю PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [Ошибка] Не удалось установить PyInstaller.
    pause
    exit /b 1
)

echo [2/4] Очищаю старые артефакты сборки (build/, dist/, *.spec)...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "*.spec" del /q "*.spec"

echo [3/4] Запускаю PyInstaller (onedir + console)...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --console ^
    --name GadgetOfTheFuture ^
    --collect-all easyocr ^
    --collect-all torch ^
    --collect-all torchvision ^
    main.py
if errorlevel 1 (
    echo.
    echo [Ошибка] Сборка завершилась с ошибкой. Смотри вывод выше.
    pause
    exit /b 1
)

echo [4/4] Копирую config.py и data\ рядом с .exe (они НЕ внутри exe)...
copy /y config.py "dist\GadgetOfTheFuture\config.py" >nul
if exist data (
    xcopy /e /i /y data "dist\GadgetOfTheFuture\data" >nul
)

echo.
echo ============================================================
echo  Готово! Исполняемый файл:
echo    dist\GadgetOfTheFuture\GadgetOfTheFuture.exe
echo.
echo  Рядом с exe уже лежат:
echo    dist\GadgetOfTheFuture\config.py
echo    dist\GadgetOfTheFuture\data\
echo.
echo  ВАЖНО: при первом запуске EasyOCR скачает свои модели
echo  (обычно в %%USERPROFILE%%\.EasyOCR\model).
echo ============================================================
echo.
pause
endlocal
