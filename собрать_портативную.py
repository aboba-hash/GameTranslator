"""
Собирает портативную версию переводчика - без самодельного .exe, который антивирусы подозревают.

Внутри: официальный встраиваемый Python с python.org (pythonw.exe подписан Python Software Foundation),
библиотеки из requirements.txt и translator.py открытым текстом. Запуск - "Переводчик.bat".

  python собрать_портативную.py
Результат: папка "сборка_портативная\\Переводчик" и архив "сборка_портативная\\Perevodchik.zip".
Собирать тем же Python 3.x, что и встраиваемая версия (берётся версия запущенного Python).
"""
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "сборка_портативная")
ROOT = os.path.join(OUT, "Переводчик")
PY = os.path.join(ROOT, "python")
APP = os.path.join(ROOT, "app")
VER = platform.python_version()
TAG = "".join(VER.split(".")[:2])            # "311"
BASE = sys.base_prefix                       # установленный Python - из него берём tkinter


def step(text):
    print(f"\n== {text}")


if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(APP)

step(f"Встраиваемый Python {VER} с python.org")
url = f"https://www.python.org/ftp/python/{VER}/python-{VER}-embed-amd64.zip"
archive = os.path.join(OUT, "embed.zip")
urllib.request.urlretrieve(url, archive)
zipfile.ZipFile(archive).extractall(PY)
os.remove(archive)

# пути поиска модулей: стандартная библиотека, библиотеки, папка с программой
with open(os.path.join(PY, f"python{TAG}._pth"), "w", encoding="utf-8") as f:
    f.write(f"python{TAG}.zip\n.\nLib\\site-packages\n..\\app\nimport site\n")

step("tkinter (окна программы) из установленного Python")
shutil.copytree(os.path.join(BASE, "Lib", "tkinter"), os.path.join(PY, "tkinter"),
                ignore=shutil.ignore_patterns("test", "__pycache__"))
for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
    shutil.copy2(os.path.join(BASE, "DLLs", name), PY)
for name in ("tcl8.6", "tk8.6"):
    shutil.copytree(os.path.join(BASE, "tcl", name), os.path.join(PY, "tcl", name),
                    ignore=shutil.ignore_patterns("demos", "images", "msgs", "tzdata"))

step("Библиотеки из requirements.txt")
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--target",
                       os.path.join(PY, "Lib", "site-packages"), "-r", os.path.join(HERE, "requirements.txt")],
                      env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))   # путь на русском
# консольные утилиты библиотек (mss.exe и т.п.) программе не нужны, а неподписанные .exe - лишний повод
# для подозрений антивируса
shutil.rmtree(os.path.join(PY, "Lib", "site-packages", "bin"), ignore_errors=True)

step("Программа")
for name in ("translator.py", "icon.ico", "проверка.py", "ОПИСАНИЕ.txt"):
    shutil.copy2(os.path.join(HERE, name), APP)
shutil.copy2(os.path.join(HERE, "README.md"), ROOT)

bat = '@echo off\r\nstart "" "%~dp0python\\pythonw.exe" "%~dp0app\\translator.py"\r\n'
with open(os.path.join(ROOT, "Переводчик.bat"), "w", encoding="cp866") as f:
    f.write(bat)
check = ('@echo off\r\nchcp 65001 >nul\r\n"%~dp0python\\python.exe" "%~dp0app\\проверка.py" %*\r\n')
with open(os.path.join(ROOT, "Проверка.bat"), "w", encoding="utf-8") as f:
    f.write(check)
with open(os.path.join(ROOT, "Как включить нейросеть.txt"), "w", encoding="utf-8-sig") as f:
    f.write("Как включить перевод по смыслу (нейросеть Groq, бесплатно)\n\n"
            "1. Зарегистрируйтесь на https://console.groq.com/keys и нажмите \"Create API Key\".\n"
            "2. Создайте в папке app (рядом с translator.py) файл ключ_groq.txt\n"
            "   и вставьте в него ключ - строку, которая начинается с gsk_\n"
            "3. Перезапустите переводчик (значок у часов -> Выход, потом снова Переводчик.bat).\n\n"
            "Без ключа всё тоже работает - переводит Google/Яндекс.\n")

step("Архив")
zpath = os.path.join(OUT, "Perevodchik.zip")
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for folder, _, files in os.walk(ROOT):
        for name in files:
            full = os.path.join(folder, name)
            z.write(full, os.path.relpath(full, OUT))
print(f"\nГотово: {ROOT}\n        {zpath} ({os.path.getsize(zpath) / 2 ** 20:.1f} МБ)")
