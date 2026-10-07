@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Устанавливаю библиотеки для переводчика...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Не получилось. Проверьте, что установлен Python 3 с галочкой "Add python.exe to PATH".
) else (
    echo.
    echo Готово. Запуск: "Запустить переводчик.bat"
)
pause
