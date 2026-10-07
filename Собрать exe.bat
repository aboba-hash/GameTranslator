@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Собираю переводчик из translator.py (1-3 минуты)...
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onedir --windowed --name GameTranslator --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." --collect-all winrt --hidden-import pystray._win32 --distpath сборка\dist --workpath сборка\build --specpath сборка translator.py
if errorlevel 1 (
    echo.
    echo Сборка не удалась - смотрите сообщения выше.
    pause
    exit /b 1
)
if exist "Переводчик" rmdir /s /q "Переводчик"
move "сборка\dist\GameTranslator" "Переводчик" >nul
ren "Переводчик\GameTranslator.exe" "Переводчик.exe"
echo.
echo Готово: папка "Переводчик" - в ней Переводчик.exe (запускается за 1-2 секунды).
echo Переносить на другой ПК нужно всю папку целиком. Папку "сборка" можно удалить.
pause
