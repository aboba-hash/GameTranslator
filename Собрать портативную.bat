@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Собираю портативную версию (1-2 минуты)...
set PYTHONIOENCODING=utf-8
python собрать_портативную.py
pause
