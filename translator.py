"""
Переводчик экрана для игр.

F8  - включить перевод: английский текст на мониторе, где мышка, закрывается
      русским переводом. Пока перевод включён, он сам обновляется, когда
      на экране что-то меняется (новое сообщение в чате, другая вкладка...).
F8 ещё раз - выключить.
F7  - выделить мышкой область: на её месте сразу появится перевод. Его можно
      скопировать картинкой или текстом, сохранить, сравнить с оригиналом.
F9  - перевод на английский (разговорный, со сленгом; Shift+F9 - обычный правильный):
      * в программах: выделите русский текст и нажмите F9 - он заменится английским;
      * в играх (и если ничего не выделено): появится плашка - печатайте по-русски,
        ниже сразу виден перевод; Enter - английский текст впечатается в чат игры.
Shift+F8 - сохранить отчёт в папку "отчёты": снимок экрана и какие строки
           перевелись, а какие нет и почему.
Ctrl+F8 - выйти из программы.

Перевод не мешает играть: клики проходят сквозь него в игру.

Важно: в игре должен стоять режим "Оконный без рамки" (Borderless / Windowed
Fullscreen). Поверх эксклюзивного полноэкранного режима Windows окна не рисует.
"""

import asyncio
import ctypes
import ctypes.wintypes
import http.client
import io
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import unicodedata
import urllib.parse
import uuid
import winreg

import mss
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

# ---------------- настройки ----------------
HOTKEY = 0x77            # F8 (коды клавиш: F6=0x75, F7=0x76, F9=0x78, F10=0x79)
REGION_HOTKEY = 0x76     # F7 - выделить область и перевести её
TYPE_HOTKEY = 0x78       # F9 - заменить выделенный текст английским переводом
SOURCE_LANG = "en"
TARGET_LANG = "ru"
TYPE_LANG = "en"         # на какой язык переводить выделенный текст по F9
UPSCALE = 2              # увеличение снимка перед распознаванием: мелкий игровой текст читается лучше
LIVE_INTERVAL = 1.0      # как часто (сек) проверять, изменился ли экран
FONT = "Segoe UI"
FONT_BOLD = "Segoe UI Semibold"
TEXT_COLOR = "#F3F4F6"
CARD_COLOR = "#161B26"   # фон карточки с переводом
BORDER_COLOR = "#2E3646"
ACCENT_COLOR = "#4F8CFF" # синяя полоска слева
OPACITY = 1.0            # прозрачность перевода (1 = непрозрачный, 0.9 = чуть видно игру)
AI_TRANSLATE = True      # F9 и F7 переводит нейросеть (Groq) - по смыслу, с контекстом и сленгом.
                         # Нет ключа, кончился лимит, нет связи - сам переходит на Google
AI_MODEL = "openai/gpt-oss-120b"
# --------------------------------------------

MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
TRANSPARENT = "#FF00FE"  # цвет-"дырка": через него видно игру

# папка программы: рядом с .exe (или с translator.py) лежат кэш, отчёты, скрины
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # координаты в реальных пикселях
except Exception:
    user32.SetProcessDPIAware()
# типы для функций буфера обмена (на 64-битной Windows без них адреса обрезаются)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.EnumClipboardFormats.restype = ctypes.c_uint
user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]

events = queue.Queue()
cache = {}

# настройки, которые меняются из меню значка в трее - запоминаются в "настройки.json"
SETTINGS_FILE = os.path.join(APP_DIR, "настройки.json")
settings = {
    "ai": True,             # переводить нейросетью (F9, F7, чат)
    "slang": True,          # F9 пишет со сленгом (Shift+F9 - наоборот)
    "key_screen": HOTKEY,   # клавиши (коды Windows); Shift/Ctrl-варианты меняются вместе с ними
    "key_region": REGION_HOTKEY,
    "key_type": TYPE_HOTKEY,
    "opacity": OPACITY,     # непрозрачность перевода поверх игры (0.4-1)
    "text_scale": 1.0,      # размер текста перевода поверх игры (0.8-1.4)
    "chat_region": None,
    "chat_hide": 10,        # через сколько секунд без новых сообщений прятать перевод чата (0 - не прятать)    # область чата [слева, сверху, справа, снизу] в координатах экрана
}
last_source = ["Google"]                 # кто перевёл последний текст: "нейросеть" или "Google"


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings.update({k: v for k, v in json.load(f).items() if k in settings})
    except (OSError, ValueError):
        pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ---------- автозапуск с Windows ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "GameTranslator"


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --autostart'


def autostart_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        elif autostart_value() is not None:
            winreg.DeleteValue(k, RUN_NAME)


def key_name(vk):
    """Название клавиши для подсказок: 0x77 -> "F8"."""
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    scan = user32.MapVirtualKeyW(vk, 0)
    buf = ctypes.create_unicode_buffer(32)
    if scan and user32.GetKeyNameTextW(scan << 16, buf, 32):
        return buf.value
    return f"#{vk}"


def resource(name):
    """Файл, вшитый в .exe (или лежащий рядом с translator.py)."""
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), name)


# ---------- горячие клавиши ----------
HOTKEY_EVENTS = {1: ("hotkey",), 2: ("quit",), 3: ("report",), 4: ("select",), 5: ("type", True),
                 6: ("type", False), 7: ("chat",), 8: ("chat_new",)}
_hotkeys = {"thread": None, "tid": 0}


def hotkey_thread(first):
    _hotkeys["tid"] = kernel32.GetCurrentThreadId()
    ks, kr, kt = settings["key_screen"], settings["key_region"], settings["key_type"]
    table = [(1, 0, ks), (2, MOD_CONTROL, ks), (3, MOD_SHIFT, ks), (4, 0, kr), (7, MOD_SHIFT, kr),
             (8, MOD_CONTROL, kr), (5, 0, kt), (6, MOD_SHIFT, kt)]
    busy = []
    for i, mod, vk in table:
        if not user32.RegisterHotKey(None, i, mod | MOD_NOREPEAT, vk):
            busy.append(("Ctrl+" if mod == MOD_CONTROL else "Shift+" if mod == MOD_SHIFT else "") + key_name(vk))
    if busy and busy[0] == key_name(ks) and first:
        events.put(("error", f"Не удалось занять клавишу {busy[0]} — её уже использует другая программа."))
        return
    if busy:
        events.put(("type_done", "Заняты другой программой: " + ", ".join(busy), "warn"))
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEY_EVENTS:
            events.put(HOTKEY_EVENTS[msg.wParam])
    for i in HOTKEY_EVENTS:
        user32.UnregisterHotKey(None, i)


def start_hotkeys(first=False):
    _hotkeys["thread"] = threading.Thread(target=hotkey_thread, args=(first,), daemon=True)
    _hotkeys["thread"].start()


def stop_hotkeys():
    """Отпускаем клавиши (на время настройки - чтобы их можно было нажать в окне настроек)."""
    t, tid = _hotkeys["thread"], _hotkeys["tid"]
    if t and tid:
        user32.PostThreadMessageW(tid, 0x0012, 0, 0)    # WM_QUIT
        t.join(1)
    _hotkeys["thread"], _hotkeys["tid"] = None, 0


def monitor_at(x, y):
    with mss.MSS() as sct:
        for m in sct.monitors[1:]:
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return dict(m)
        return dict(sct.monitors[1])


# ---------- снимок экрана ----------
def monitor_under_cursor(sct):
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    for mon in sct.monitors[1:]:
        if mon["left"] <= pt.x < mon["left"] + mon["width"] and mon["top"] <= pt.y < mon["top"] + mon["height"]:
            return mon
    return sct.monitors[1]


def cursor_monitor():
    with mss.mss() as sct:
        return monitor_under_cursor(sct)


def grab(mon):
    with mss.mss() as sct:
        shot = sct.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def changed_pixels(a, b):
    """Сколько точек отличается у маленьких чёрно-белых копий снимков - дёшево по процессору."""
    if a is None or b is None:
        return 10 ** 9
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 24 else 0)
    return diff.histogram()[255]


def hwnd_of(win):
    win.update_idletasks()
    return int(win.wm_frame(), 0)


user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_uint]
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_KEEP = 0x0001 | 0x0002 | 0x0010 | 0x0200       # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER


def raise_topmost(win):
    """Поднимаем окно наверх среди окон "поверх всех": игры в режиме без рамки, оверлеи Discord/Steam
    и панель задач тоже бывают "поверх всех" и перекрывают тех, кто поднялся раньше."""
    try:
        if win.winfo_exists() and win.winfo_viewable():
            user32.SetWindowPos(int(win.wm_frame(), 0), HWND_TOPMOST, 0, 0, 0, 0, SWP_KEEP)
    except tk.TclError:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin():
    """Перезапуск от имени администратора: нужен, если игра запущена от администратора - иначе
    Windows не даёт обычной программе печатать в неё (F9) и не пропускает ей клавиши."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, "--restarted"
    else:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        params = f'"{os.path.abspath(__file__)}" --restarted'
    if ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, APP_DIR, 1) > 32:
        events.put(("quit_now",))       # новая копия запущена - эту закрываем
    else:
        events.put(("status", "Перезапуск от имени администратора отменён"))


def exclude_from_capture(win):
    """Прячем своё окно от снимков экрана, чтобы не распознавать собственный перевод."""
    return bool(user32.SetWindowDisplayAffinity(hwnd_of(win), WDA_EXCLUDEFROMCAPTURE))


def make_clickthrough(win):
    """Клики и клавиатура проходят сквозь окно в игру, окно не забирает фокус."""
    hwnd = hwnd_of(win)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


# ---------- распознавание (встроенный OCR Windows) ----------
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN = re.compile(r"[A-Za-z]")
# русские буквы, которые не спутать с английскими (а/с/е/о/р/х/к/м/н/т/в/п/д/г/и/у/ь
# русский распознаватель любит "видеть" в английском тексте: "arc" -> "асс", "TOP" -> "ТОР")
DISTINCT_CYR = re.compile(r"[бжзлфцчшщъыэюяйёБГДЖЗИЙЛПФЦЧШЩЪЫЭЮЯЁ]")
# так английский распознаватель "читает" русский текст: "tvqecTe", "MT06bl" - заглавная
# после строчной или цифра посреди слова
GARBLED = re.compile(r"[a-z][A-Z]|[A-Za-z][0-9][A-Za-z]")
WORD = re.compile(r"[A-Za-z']+")
VOWEL = re.compile(r"[aeiouyAEIOUY]")
# ---------- разговорные слова и игровой сленг ----------
# короткие реплики целиком - сразу готовый русский перевод (Google их переводит плохо)
SLANG_PHRASES = {
    "gg": "хорошая игра", "gg wp": "хорошая игра, хорошо сыграно", "ggwp": "хорошая игра, хорошо сыграно",
    "wp": "хорошо сыграно", "gl": "удачи", "hf": "веселись", "gl hf": "удачи и веселья",
    "glhf": "удачи и веселья", "lol": "ахах", "lmao": "ахахах", "rofl": "ахахаха", "xd": "ахах",
    "afk": "отошёл (AFK)", "brb": "скоро вернусь", "ty": "спасибо", "thx": "спасибо", "tnx": "спасибо",
    "ty all": "всем спасибо", "np": "без проблем", "ez": "изи, легко", "ez pz": "изи, легкотня",
    "k": "ок", "kk": "ок", "ok": "ок", "okay": "ок", "omg": "боже мой", "wtf": "что за фигня",
    "idk": "не знаю", "nvm": "неважно", "gj": "молодец", "nt": "хорошая попытка", "ns": "хороший выстрел",
    "bruh": "бро...", "rip": "помянем", "pog": "круто!", "poggers": "круто!", "sus": "подозрительно",
    "ff": "сдаёмся", "gn": "спокойной ночи", "cya": "увидимся", "bb": "пока", "sry": "извини",
    "sorry": "извини", "yw": "пожалуйста", "jk": "шучу", "ikr": "вот-вот", "fr": "реально",
    "no cap": "без шуток", "noob": "нуб", "u ok": "ты в порядке?", "wb": "с возвращением",
    "hi": "привет", "hey": "привет", "hello": "привет", "yo": "здорово", "sup": "как дела?",
    "wassup": "как дела?", "whats up": "как дела?", "ty gg": "спасибо, хорошая игра",
}
# слова, которые перед переводом расшифровываем в обычный английский
SLANG_WORDS = {
    "u": "you", "ur": "your", "r": "are", "ya": "you", "yall": "you all", "pls": "please", "plz": "please",
    "plss": "please", "thx": "thanks", "ty": "thank you", "np": "no problem", "idk": "I don't know",
    "imo": "in my opinion", "imho": "in my opinion", "btw": "by the way", "omg": "oh my god",
    "wtf": "what the hell", "lol": "haha", "lmao": "haha", "brb": "be right back",
    "afk": "away from keyboard", "gg": "good game", "wp": "well played", "ez": "easy", "gl": "good luck",
    "nvm": "never mind", "ofc": "of course", "tho": "though", "cuz": "because", "coz": "because",
    "bc": "because", "rn": "right now", "atm": "at the moment", "asap": "as soon as possible",
    "wanna": "want to", "gonna": "going to", "gotta": "have to", "dunno": "don't know", "lemme": "let me",
    "gimme": "give me", "kinda": "kind of", "sorta": "sort of", "ain't": "is not", "aint": "is not",
    "yea": "yeah", "ye": "yes", "yep": "yes", "yup": "yes", "nah": "no", "nope": "no", "k": "okay",
    "kk": "okay", "m8": "mate", "noob": "newbie", "n00b": "newbie", "nub": "newbie", "noobs": "newbies",
    "op": "overpowered", "dmg": "damage", "hp": "health", "mp": "mana", "xp": "experience",
    "exp": "experience", "lvl": "level", "lvls": "levels", "inv": "inventory", "atk": "attack",
    "tp": "teleport", "rez": "resurrect", "mob": "monster", "mobs": "monsters",
    "smh": "shaking my head", "tbh": "to be honest", "irl": "in real life", "dm": "private message",
    "w8": "wait", "gr8": "great", "b4": "before", "cya": "see you",
    "sry": "sorry", "srry": "sorry", "ikr": "I know, right", "fr": "for real", "ngl": "not going to lie",
    "lmk": "let me know", "jk": "just kidding", "gj": "good job", "rq": "real quick", "msg": "message",
    "ppl": "people", "smth": "something", "sth": "something", "pic": "picture", "wat": "what",
    "wut": "what", "dat": "that", "dis": "this", "im": "I'm", "dont": "don't", "cant": "can't",
    "wont": "won't", "didnt": "didn't", "isnt": "isn't", "thats": "that's", "whats": "what's",
    "ive": "I've", "youre": "you're", "theyre": "they're", "doesnt": "doesn't", "wasnt": "wasn't",
    "shouldnt": "shouldn't", "couldnt": "couldn't", "wouldnt": "wouldn't", "lets": "let's",
    "luv": "love", "bf": "boyfriend", "gf": "girlfriend", "bday": "birthday", "tmrw": "tomorrow",
    "2day": "today", "2morrow": "tomorrow", "tonite": "tonight", "pvp": "PvP", "pve": "PvE",
    "nerf": "weaken", "nerfed": "weakened", "buffed": "strengthened", "rekt": "destroyed",
    "pwned": "destroyed", "ty4": "thanks for", "thnx": "thanks", "cus": "because", "bruh": "bro",
}
SHORT_WORDS = {"ok", "go", "no", "hi", "up", "on", "yes"} | set(SLANG_WORDS) | set(SLANG_PHRASES)
# начало сообщения в чате: "[Ник]:", "<Ник>", "[12:30]", "Nick_123:"
# (распознаватель часто читает "]" как "1" или "l": "[Player1231:" - это тоже ник)
CHAT_PREFIX = re.compile(r"^((?:\[[^\]]{1,30}\]\s*)+:?\s*|\[[^\]:]{1,30}:\s*|<[^>]{1,30}>\s*:?\s*"
                         r"|[A-Za-z0-9_.\-]*[0-9_][A-Za-z0-9_.\-]*:\s+)")
TIME_PREFIX = re.compile(r"^\(?\d{1,2}:\d{2}")


def gray_bitmap(img):
    """Чёрно-белая картинка для распознавателя: в 4 раза меньше данных, чем цветная, и читается не хуже."""
    writer = DataWriter()
    writer.write_bytes(img.tobytes())
    bitmap = SoftwareBitmap(BitmapPixelFormat.GRAY8, img.width, img.height, BitmapAlphaMode.IGNORE)
    bitmap.copy_from_buffer(writer.detach_buffer())
    return bitmap


async def _recognize(img):
    """Возвращает (результаты английского распознавания для нескольких вариантов снимка, русские слова)."""
    # у каждого прохода свой распознаватель - тогда они работают одновременно, а не по очереди
    engines = [OcrEngine.try_create_from_language(Language(SOURCE_LANG)) for _ in range(3)]
    if engines[0] is None:
        raise RuntimeError("В Windows не установлен английский язык для распознавания текста.")
    # русский распознаватель - чтобы узнать, где на экране уже русский текст
    ru_engine = OcrEngine.try_create_from_language(Language(TARGET_LANG))
    if UPSCALE != 1:
        img = img.resize((img.width * UPSCALE, img.height * UPSCALE), Image.BILINEAR)
    gray = img.convert("L")
    # красные волнистые подчёркивания проверки орфографии (Блокнот, браузер) распознаватель
    # принимает за части букв и выдаёт "ygu.c in.ugn.tgcy" вместо "your inventory".
    # Явно красные пиксели делаем светлыми (берём красный канал) - на тёмном фоне красный текст
    # от этого только ярче, а подчёркивания на светлом фоне исчезают
    r, g, b = img.convert("RGB").split()
    red = ImageChops.subtract(r, ImageChops.lighter(g, b)).point(lambda v: 255 if v > 60 else 0)
    main = gray_bitmap(Image.composite(r, gray, red))
    # текст поверх картинок распознаётся плохо: фон "сливается" с буквами. Поэтому ещё два
    # прохода по копиям, где оставлен только очень светлый или только очень тёмный текст
    bright = gray_bitmap(gray.point(lambda v: 0 if v > 200 else 255))
    dark = gray_bitmap(gray.point(lambda v: 0 if v < 60 else 255))
    jobs = [engines[0].recognize_async(main), engines[1].recognize_async(bright),
            engines[2].recognize_async(dark)]
    if ru_engine is not None:
        jobs.append(ru_engine.recognize_async(main))
    done = await asyncio.gather(*jobs)
    ru_words = []
    if ru_engine is not None:
        ru_words = [(w.bounding_rect, w.text) for l in done[3].lines for w in l.words]
    return list(done[:3]), ru_words


def split_line(words):
    """Windows склеивает в одну строку надписи, стоящие на одной высоте в разных местах экрана.
    Режем строку там, где между словами большой промежуток."""
    words = sorted(words, key=lambda w: w.bounding_rect.x)
    heights = sorted(w.bounding_rect.height for w in words)
    h = heights[len(heights) // 2]
    parts = [[words[0]]]
    for prev, w in zip(words, words[1:]):
        gap = w.bounding_rect.x - (prev.bounding_rect.x + prev.bounding_rect.width)
        if gap > 2.5 * h:
            parts.append([w])
        else:
            parts[-1].append(w)
    return parts


def looks_english(text):
    """Отсекаем мусор, который распознаватель "видит" в картинках и узорах."""
    words = WORD.findall(text)
    if not words:
        return False
    # кружочки, палочки и крестики на картинках распознаются как "OVO", "Il", "XOX"
    if all(re.fullmatch(r"[OoVvXxIil]+", w) for w in words):
        return False
    good = [w for w in words if (len(w) >= 2 and VOWEL.search(w) and not GARBLED.search(w))
            or w.lower() in SLANG_WORDS]
    if not any(len(w) >= 3 for w in good) and not any(w.lower() in SHORT_WORDS for w in words):
        return False
    letters = sum(c.isalpha() for c in text)
    visible = sum(not c.isspace() for c in text)
    return len(good) >= 0.5 * len(words) and letters >= 0.5 * visible


def russian_text(ru_words, x1, y1, x2, y2):
    """Если русский распознаватель видит на месте строки русский текст - возвращает его."""
    words = [w for r, w in ru_words
             if x1 <= r.x + r.width / 2 <= x2 and y1 <= r.y + r.height / 2 <= y2]
    joined = " ".join(words)
    if len(CYRILLIC.findall(joined)) > len(LATIN.findall(joined)):
        return joined
    return None


def starts_message(text):
    return bool(CHAT_PREFIX.match(text) or TIME_PREFIX.match(text))


def merge_rows(segs):
    """Распознаватель иногда режет одну строку на куски ("text-decoration:" и "none;").
    Склеиваем обратно куски, стоящие рядом на одной высоте."""
    rows = []
    for s in sorted(segs, key=lambda s: s["box"][0]):
        x1, y1, x2, y2 = s["box"]
        h = max(1, y2 - y1)
        for r in rows:
            rx1, ry1, rx2, ry2 = r["box"]
            rh = max(1, ry2 - ry1)
            same_row = abs((y1 + y2) - (ry1 + ry2)) / 2 < 0.35 * max(h, rh)
            near = -0.5 * h <= x1 - rx2 <= 2.5 * max(h, rh)
            if same_row and near and 0.6 <= h / rh <= 1.6:
                r["text"] += " " + s["text"]
                r["box"] = (min(rx1, x1), min(ry1, y1), max(rx2, x2), max(ry2, y2))
                break
        else:
            rows.append(dict(s))
    return rows


def group(segs):
    """Собираем строки одного абзаца (текст в окошке диалога, описание предмета...) в блок,
    чтобы переводить целые предложения, а не обрывки. Сообщения чата не склеиваем."""
    blocks = []
    for s in sorted(segs, key=lambda s: (s["box"][1], s["box"][0])):
        x1, y1, x2, y2 = s["box"]
        h = max(1, y2 - y1)
        target = None
        if not starts_message(s["text"]):
            for b in reversed(blocks[-8:]):
                # строка закончилась точкой и т.п. - следующая начинает новую мысль, не склеиваем:
                # так перевод ляжет ровно на свои строки
                if re.search(r"[.!?;:{}]\s*$", b["text"]):
                    continue
                bx1, by1, bx2, by2 = b["box"]
                lx1, ly1, lx2, ly2 = b["lines"][-1]
                lh = max(1, ly2 - ly1)
                gap = y1 - ly2
                same_size = 0.7 <= h / lh <= 1.4
                close = -0.3 * lh <= gap <= 0.8 * max(h, lh)
                # строка должна стоять ровно под предыдущей (по левому краю или по центру)
                # и заметно перекрываться с ней - иначе это отдельные надписи на картинке
                aligned = abs(x1 - lx1) <= lh or abs((x1 + x2) - (lx1 + lx2)) / 2 <= lh
                overlap = min(x2, lx2) - max(x1, lx1)
                overlaps = overlap > 0.5 * min(x2 - x1, lx2 - lx1)
                if same_size and close and aligned and overlaps and len(b["lines"]) < 15:
                    target = b
                    break
        if target is None:
            blocks.append({"text": s["text"], "box": [x1, y1, x2, y2], "lines": [s["box"]]})
            continue
        t = target["text"]
        if t.endswith("-") and s["text"][:1].islower():
            target["text"] = t[:-1] + s["text"]       # перенос слова: "some-" + "thing"
        else:
            target["text"] = t + " " + s["text"]
        b = target["box"]
        target["box"] = [min(b[0], x1), min(b[1], y1), max(b[2], x2), max(b[3], y2)]
        target["lines"].append(s["box"])
    return blocks


def plain(text):
    """Распознаватель иногда ставит в английские слова "ö", "é", "å" - убираем значки."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def quality(text):
    """Насколько текст похож на нормальный английский: длина "хороших" слов минус мусорные знаки.
    Ник в начале ("[xXSniperXx]:") не оцениваем - ники бывают какие угодно."""
    m = CHAT_PREFIX.match(text)
    if m:
        text = text[len(m.group(0)):]
    good = sum(len(w) for w in WORD.findall(text)
               if len(w) >= 2 and VOWEL.search(w) and not GARBLED.search(w))
    return (good - 2 * len(re.findall(r"[^\w\s.,!?:;\-\[\]()<>\"{}#%=/*+']", text))
            - 3 * len(GARBLED.findall(text)))


def overlaps(a, b):
    """Пересекаются ли прямоугольники больше чем на 40% меньшего из них."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h > 0.4 * max(1, smaller)


def defective(text):
    """Явная ошибка распознавания в тексте (без ника): цифра посреди слова, мусорные знаки."""
    m = CHAT_PREFIX.match(text)
    if m:
        text = text[len(m.group(0)):]
    return bool(GARBLED.search(text) or re.search(r"[^\w\s.,!?:;\-\[\]()<>\"{}#%=/*+']", text))


def contains(a, b):
    """Прямоугольник a накрывает b (с запасом в 30% высоты строки)."""
    tol = 0.3 * max(1, b[3] - b[1])
    return a[0] <= b[0] + tol and a[1] <= b[1] + tol and a[2] >= b[2] - tol and a[3] >= b[3] - tol


def wider(a, b):
    """a шире b хотя бы на высоту строки - значит, в a есть слова, которых нет в b."""
    return (a[2] - a[0]) - (b[2] - b[0]) >= (b[3] - b[1])


def letters(text):
    return sum(c.isalpha() for c in text)


def same_place(a, b):
    """Прямоугольники почти совпадают (пересечение больше 70% объединения)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h
    return w * h > 0.7 * union


def analyze(img):
    """Возвращает (все найденные куски текста с причиной пропуска, блоки для перевода)."""
    results, ru_words = asyncio.run(_recognize(img))
    segs = []
    passes = []
    for result in results:
        found = []
        for line in result.lines:
            for words in split_line(list(line.words)):
                text = plain(" ".join(w.text for w in words).strip())
                rects = [w.bounding_rect for w in words]
                box = (min(r.x for r in rects), min(r.y for r in rects),
                       max(r.x + r.width for r in rects), max(r.y + r.height for r in rects))
                found.append({"text": text, "words": words, "box": box, "q": quality(text),
                              "ok": looks_english(text)})
        passes.append(found)
    # основной - обычный проход: он даёт целые строки. Чёрно-белые проходы только добавляют
    # то, чего в нём нет (подписи на картинках), или заменяют явный мусор на нормальный текст
    chosen = list(passes[0])
    for extra in passes[1:]:
        for e in sorted(extra, key=lambda f: -f["q"]):
            if not e["ok"]:
                continue
            hit = [c for c in chosen if overlaps(e["box"], c["box"])]
            if not hit:
                chosen.append(e)
            elif e["q"] > max(c["q"] for c in hit) and (
                    all(not c["ok"] for c in hit)
                    # та же надпись на том же месте, но прочитана чище ("Dark Forest" вместо "Dark4Forest")
                    or (len(hit) == 1 and same_place(e["box"], hit[0]["box"])
                        and defective(hit[0]["text"]) and not defective(e["text"])
                        and letters(e["text"]) >= letters(hit[0]["text"]) - 1)
                    # та же строка, но прочитана целиком ("Danger! Wolves..." вместо ". Wolves...")
                    # (вариант должен быть заметно шире - захватывать слова, которых в основном нет;
                    # просто "больше букв" не годится: "stornn canoe" длиннее, чем "storm came")
                    or (len(hit) == 1 and contains(e["box"], hit[0]["box"])
                        and wider(e["box"], hit[0]["box"])
                        and not defective(e["text"]) and letters(e["text"]) > letters(hit[0]["text"]) + 2)):
                chosen = [c for c in chosen if c not in hit] + [e]
    for c in chosen:
        text, words = c["text"], c["words"]
        x1, y1, x2, y2 = c["box"]
        # ник в начале ("[Tank]: ok") не учитываем, проверяем на русский только само сообщение
        m = CHAT_PREFIX.match(text)
        msg_words = words
        if m:
            n, msg_words = 0, []
            for w in words:
                n += len(w.text) + 1
                if n > len(m.group(0)):
                    msg_words.append(w)
        mx1 = min((w.bounding_rect.x for w in msg_words), default=x1)
        ru = russian_text(ru_words, mx1, y1, x2, y2)
        skip = None
        if not looks_english(text):
            skip = "не похоже на английский текст"
        elif ru and (DISTINCT_CYR.search(ru) or GARBLED.search(text)):
            skip = f"русский текст: {ru}"
        box = (int(x1 / UPSCALE), int(y1 / UPSCALE), int(x2 / UPSCALE), int(y2 / UPSCALE))
        segs.append({"text": text, "box": box, "skip": skip})
    return segs, group(merge_rows([s for s in segs if not s["skip"]]))


# ---------- перевод ----------
GOOGLE_HOST = "translate.googleapis.com"
YANDEX_HOST = "translate.yandex.net"
CACHE_FILE = os.path.join(APP_DIR, "кэш_переводов.json")
CACHE_LIMIT = 5000
FORM = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"}
_conns = {}              # открытые соединения: сервер -> соединение
_conn_lock = threading.Lock()
_cache_lock = threading.Lock()
_last_used = [0.0]       # когда последний раз реально переводили (для поддержания соединения)
_google_off_until = [0.0]   # Google временно не пускает ("необычный трафик") - до этого времени не трогаем
YANDEX_ID = uuid.uuid4().hex


class Blocked(Exception):
    pass


def _post(host, path, body):
    """POST по одному и тому же открытому соединению: так вдвое быстрее, чем каждый раз заново
    "здороваться" с сервером (особенно через VPN)."""
    with _conn_lock:
        for attempt in range(2):
            try:
                if host not in _conns:
                    _conns[host] = http.client.HTTPSConnection(host, timeout=10)
                _conns[host].request("POST", path, body=body, headers=FORM)
                resp = _conns[host].getresponse()
                return resp.status, resp.read()
            except (http.client.HTTPException, OSError):
                # сервер закрыл соединение, пока мы молчали - открываем заново и пробуем ещё раз
                old = _conns.pop(host, None)
                if old is not None:
                    old.close()
                if attempt:
                    raise


def _google(text, sl=SOURCE_LANG, tl=TARGET_LANG, spell=False):
    """spell=True - Google сам исправит опечатки перед переводом ("превет как дила")."""
    path = f"/translate_a/single?client=gtx&sl={sl}&tl={tl}&dt=t" + ("&dt=qca" if spell else "")
    status, data = _post(GOOGLE_HOST, path, urllib.parse.urlencode({"q": text}))
    if status in (302, 403, 429):
        raise Blocked(f"Google временно не пускает (ошибка {status})")
    if status != 200:
        raise RuntimeError(f"Google Переводчик ответил ошибкой {status}")
    answer = json.loads(data.decode("utf-8"))
    return "".join(part[0] for part in answer[0] if part[0])


def _yandex(texts, sl, tl):
    """Запасной переводчик - Яндекс. Переводит сразу список строк."""
    lang = tl if sl == "auto" else f"{sl}-{tl}"
    body = urllib.parse.urlencode([("text", t) for t in texts])
    status, data = _post(YANDEX_HOST, f"/api/v1/tr.json/translate?id={YANDEX_ID}-0-0&srv=android&lang={lang}", body)
    if status != 200:
        raise RuntimeError(f"Яндекс Переводчик ответил ошибкой {status}")
    return json.loads(data.decode("utf-8"))["text"]


def yandex_spell(text):
    """Исправляем опечатки в русском тексте (Яндекс.Спеллер) - нужно, когда Google недоступен:
    Google исправляет опечатки сам, а Яндекс-переводчик нет ("превет как дила")."""
    status, data = _post("speller.yandex.net", "/services/spellservice.json/checkText",
                         urllib.parse.urlencode({"text": text, "lang": "ru", "options": 2 | 4}))
    if status != 200:
        return text
    for fix in sorted(json.loads(data.decode("utf-8")), key=lambda f: -f["pos"]):
        if fix.get("s") and fix["len"] >= 3:
            text = text[:fix["pos"]] + fix["s"][0] + text[fix["pos"] + fix["len"]:]
    return text


def online_translate(texts, sl=SOURCE_LANG, tl=TARGET_LANG, spell=False):
    """Переводим список строк: сначала Google, если он не пускает или недоступен - Яндекс."""
    if time.time() > _google_off_until[0]:
        try:
            if len(texts) == 1:
                return [_google(texts[0], sl, tl, spell)]
            # одним запросом все строки сразу; если строки "съехали" - переводим по одной
            joined = _google("\n".join(texts), sl, tl, spell).split("\n")
            if len(joined) == len(texts):
                return joined
            return [_google(t, sl, tl, spell) for t in texts]
        except Exception:
            _google_off_until[0] = time.time() + 10 * 60   # 10 минут не тратим время на Google
    if spell and sl == "ru":
        try:
            texts = [yandex_spell(t) for t in texts]
        except Exception:
            pass
    return _yandex(texts, sl, tl)


# ---------- перевод нейросетью (Groq) ----------
# Ключ - у каждого свой (бесплатный лимит делится на всех, кто пользуется одним ключом):
# вводится в окне "Настройки…" и хранится в файле "ключ_groq.txt" рядом с программой
# (или переменная окружения GROQ_API_KEY). Получить бесплатно: https://console.groq.com/keys
KEY_FILE = os.path.join(APP_DIR, "ключ_groq.txt")
KEY_URL = "https://console.groq.com/keys"
AI_HOST = "api.groq.com"
AI_TO_EN = ("You translate Russian gaming-chat messages into English. Convey the meaning and intent, not the words: "
            "understand slang, gamer jargon (катка = match, мид = mid lane, лайн = lane, тащить = carry, "
            "слиться = give up, ливнуть = leave the game), typos and context. Keep profanity and insults at the same "
            "strength with natural English equivalents (пиздато = fucking awesome, хуево = like shit, "
            "иди нахуй = go fuck yourself). ")
AI_CASUAL = ("Write it like a native English-speaking gamer would type in chat: casual, short, lowercase, common "
             "chat abbreviations where natural (idk, u, rn, gg, wp). Output ONLY the translation, nothing else.")
AI_FORMAL = ("Write correct, natural English with normal capitalization and no chat abbreviations. "
             "Output ONLY the translation, nothing else.")
AI_TO_RU = ("You translate text from a screenshot of a game or program into natural Russian. The lines were read by "
            "OCR from one screen, so use them together as context, and silently fix obvious OCR mistakes "
            "(\"inuentory\" = inventory, \"101\" or \"l0l\" in chat = lol). Translate by meaning, the way a Russian game localization would. Keep "
            "names, numbers, hotkeys, file names and code as they are; translate slang and profanity with the same "
            "strength. Answer with JSON {\"t\": [...]} - one Russian string per input line, same count and order.")
_ai_lock = threading.Lock()
_ai_conn = [None]
_ai_off_until = [0.0]    # нейросеть временно отключена (лимит, нет связи) - до этого времени сразу Google


class AIUnavailable(Exception):
    pass


def ai_key():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        try:
            with open(KEY_FILE, encoding="utf-8-sig") as f:
                key = next((l.strip().strip('"') for l in f if l.strip().startswith("gsk_")), "")
        except OSError:
            pass
    return key


def save_key(key):
    key = key.strip().strip('"')
    if key:
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key + "\n")
    elif os.path.exists(KEY_FILE):
        os.remove(KEY_FILE)
    _ai_off_until[0] = 0


def ai_off(seconds, why):
    """Отключаем нейросеть на время и один раз говорим об этом."""
    if time.time() > _ai_off_until[0]:
        events.put(("type_done", f"Нейросеть {why} — перевожу обычным переводчиком", "warn"))
    _ai_off_until[0] = time.time() + seconds


def ai_chat(system, user, timeout, json_mode=False):
    """Запрос к нейросети. Если она недоступна - AIUnavailable (и она отключается на время)."""
    if not (AI_TRANSLATE and settings["ai"]) or time.time() < _ai_off_until[0]:
        raise AIUnavailable()
    key = ai_key()
    if not key:
        # ключа нет - работаем через Google и один раз подсказываем, где его ввести
        _ai_off_until[0] = time.time() + 10 ** 9
        events.put(("type_done", "Нейросеть не подключена — свой бесплатный ключ: значок у часов → Настройки", "info"))
        raise AIUnavailable()
    body = {"model": AI_MODEL, "temperature": 0.2, "max_tokens": 4000, "reasoning_effort": "low",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    with _ai_lock:
        status = data = retry = None
        for attempt in range(2):
            try:
                if _ai_conn[0] is None:
                    _ai_conn[0] = http.client.HTTPSConnection(AI_HOST, timeout=timeout)
                _ai_conn[0].timeout = timeout
                if _ai_conn[0].sock is not None:
                    _ai_conn[0].sock.settimeout(timeout)
                _ai_conn[0].request("POST", "/openai/v1/chat/completions", json.dumps(body), headers)
                resp = _ai_conn[0].getresponse()
                status, data, retry = resp.status, resp.read(), resp.getheader("retry-after")
                break
            except (http.client.HTTPException, OSError):
                if _ai_conn[0] is not None:
                    _ai_conn[0].close()
                    _ai_conn[0] = None
                if attempt:
                    ai_off(120, "не отвечает")
                    raise AIUnavailable()
    if status == 429:
        try:
            wait = float(retry)
        except (TypeError, ValueError):
            wait = 600
        ai_off(max(60, wait), "исчерпала лимит")
        raise AIUnavailable()
    if status in (401, 403):
        ai_off(10 ** 9, "не принимает ключ")
        raise AIUnavailable()
    if status != 200:
        ai_off(120, f"ответила ошибкой {status}")
        raise AIUnavailable()
    text = (json.loads(data.decode("utf-8"))["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise AIUnavailable()
    return text


def ai_to_en(text, casual):
    out = ai_chat(AI_TO_EN + (AI_CASUAL if casual else AI_FORMAL), text, timeout=6)
    return out.strip().strip('"')


def ai_to_ru(lines):
    """F7: все строки области одним запросом - нейросеть видит общий контекст."""
    out = ai_chat(AI_TO_RU, json.dumps(lines, ensure_ascii=False), timeout=12, json_mode=True)
    result = json.loads(out).get("t")
    if not isinstance(result, list) or len(result) != len(lines):
        raise AIUnavailable()        # ответ не по формату - этот раз переводит Google
    return [str(r).strip() for r in result]


# ---------- F9: русский сленг -> английский ----------
# русский чат-сленг, который Google переводит неправильно ("изи катка" -> "easy skating rink").
# Перед переводом заменяем на обычные слова (или сразу на английский игровой термин)
RU_SLANG = {
    "щас": "сейчас", "ща": "сейчас", "щя": "сейчас", "щаз": "сейчас", "счас": "сейчас",
    "спс": "спасибо", "спасиб": "спасибо", "пасиб": "спасибо", "пасиба": "спасибо", "сенкс": "спасибо",
    "пж": "пожалуйста", "пжл": "пожалуйста", "пжлст": "пожалуйста", "пжлста": "пожалуйста",
    "плз": "пожалуйста", "плиз": "пожалуйста", "пож": "пожалуйста", "пожалуста": "пожалуйста",
    "норм": "нормально", "нормас": "нормально", "хз": "не знаю", "мб": "может быть", "кст": "кстати",
    "кста": "кстати", "оч": "очень", "чо": "что", "че": "что", "чё": "что", "шо": "что", "ниче": "ничего",
    "ничё": "ничего", "чел": "чувак", "чела": "чувака", "челу": "чуваку", "челы": "люди", "челик": "чувак",
    "тя": "тебя", "мя": "меня", "прив": "привет", "привки": "привет", "хай": "привет", "здарова": "привет",
    "покеда": "пока", "бб": "пока", "лан": "ладно", "ладн": "ладно", "нзч": "не за что", "забей": "забудь",
    "сорян": "извини", "сори": "извини", "сорри": "извини", "рил": "реально", "рили": "реально",
    "ок": "ок", "окей": "ок", "оки": "ок", "окс": "ок", "го": "давай", "гоу": "давай",
    "изи": "легко", "изишно": "легко", "имба": "слишком сильный", "имбовый": "слишком сильный",
    "катка": "матч", "катку": "матч", "катки": "матча", "катке": "матче", "каткой": "матчем",
    "слился": "сдался", "слилась": "сдалась", "слились": "сдались", "сливаемся": "сдаёмся",
    "ливнул": "вышел из игры", "ливнула": "вышла из игры", "ливнули": "вышли из игры", "ливаю": "выхожу из игры",
    "тащит": "carries", "тащу": "carry", "тащишь": "carry", "тащил": "carried", "тащи": "carry",
    "затащил": "carried", "тащим": "carry",
    "хильни": "вылечи", "хилни": "вылечи", "хиль": "лечи", "хилить": "лечить", "хилер": "лекарь", "хилка": "лечение",
    "кд": "cooldown", "откат": "cooldown", "ульта": "ultimate", "ульту": "ultimate", "ульты": "ultimate", "ультой": "ultimate",
    "хп": "HP", "мана": "мана", "тима": "команда", "тиму": "команду", "тимой": "командой", "тиме": "команде",
    "мид": "mid", "миде": "mid", "мида": "mid", "лайн": "lane", "лайна": "lane", "лайне": "lane", "лайну": "lane",
    "тиммейт": "союзник", "тиммейты": "союзники", "тиммейтов": "союзников",
    "нуб": "noob", "нубы": "noobs", "нубас": "noob", "нубов": "noobs", "нубище": "noob",
    "рандом": "случайный", "рандомы": "случайные игроки", "пати": "party", "инвайт": "invite",
    "инвайтни": "пригласи", "кинь": "отправь", "агр": "aggro", "агрить": "агрить (aggro)", "лут": "добыча",
    "лутать": "собирать добычу", "фарм": "farm", "фармить": "фармить (farm)", "апнул": "повысил уровень",
    "апнулся": "повысил уровень", "дс": "Discord", "дискорд": "Discord", "тг": "Telegram", "лс": "личные сообщения",
    "бро": "бро", "братан": "бро", "кринж": "кринж", "лол": "lol", "кек": "lol", "рофл": "lol", "ахах": "haha",
    "ахаха": "haha", "хах": "haha", "гг": "gg", "вп": "wp", "афк": "AFK", "донат": "донат", "сек": "секунд", "мин": "минут", "инет": "интернет", "комп": "компьютер", "инфа": "информация",
}
# русский мат: Google переводит его часто наоборот ("пиздато" -> "it's fucked up") или дословно
# ("нахуй иди" -> "fuck you go"). Перед переводом заменяем на английский мат с тем же смыслом -
# английские вставки Google оставляет как есть и строит вокруг них фразу.
# Порядок важен: сначала устойчивые фразы, потом отдельные слова.
_GO = r"(?:иди|идите|пош[её]л|пошла|пошли|вали|валите|катись)"
_TO = r"(?:на\s*хуй|на\s*х[еуя]р|в\s*пизду|в\s*жопу)"
RU_MAT = [
    (_GO + r"\s+" + _TO, "go fuck yourself"), (_TO + r"\s+" + _GO, "go fuck yourself"),
    (r"пош[её]л\s+ты|пошла\s+ты|пошли\s+вы", "fuck you"),
    (r"(?:как(?:ого|ова)|какой)\s+(?:хуя|хера|хрена|черта|чёрта)", "what the fuck"),
    (r"на\s*хуя|на\s*хера|на\s*хрена|н[ао]хуя|н[ао]хера", "why the fuck"),
    (r"(?:ни\s*хуя|нихуя|ни\s*хера|нихера)\s+не", "ни хрена не"),
    (r"ни\s*хуя|нихуя|ни\s*хера|нихера", "fuck all"),
    (r"(?:мне\s+|нам\s+)?(?:по\s*хуй|похуй|похер)", "I don't give a fuck"),
    (r"(?:тебе|те)\s+пизда", "you're fucked"), (r"нам\s+пизда", "we're fucked"),
    (r"(?:ему|ей|им|вам)\s+пизда", "they're fucked"),
    (r"(?:ты|вы)\s+(?:о|а)ху[еи]л[аи]?", "have you lost your fucking mind"),
    (r"(?:о|а)ху[еи]л[аи]?", "lost his fucking mind"),
    (r"от[ъь]?еб[иа]сь|от[ъь]?ебитесь", "fuck off"),
    (r"с[ъь]?еба(?:л|ла|ли)(?:ся|ась|ись)?|с[ъь]?ебись|с[ъь]?ебывай", "fucked off"),
    (r"(?:ты|вы)\s+(?:меня\s+)?заеба(?:л|ла|ли)(?:\s+меня)?|(?:меня\s+)?заеба(?:л|ла|ли)\s+(?:ты|вы)",
     "I'm fucking sick of you"),
    (r"(?:меня\s+)?заеба(?:л|ла|ли|ло)(?:\s+меня)?", "I'm fucking sick of it"),
    (r"заебись|заебок|заебца", "fucking great"),
    # "ахуенно сыграли" -> "сыграли fucking awesome": так Google ставит слова в нужном порядке
    (r"(пиздато|[оа]ху[еи]нн?о|[оа]хуительно)\s+([а-яё]+)", r"\2 fucking awesome"),
    (r"пиздат\w*|[оа]ху[еи]нн?\w*|[оа]хуительн\w*|[оа]хренительн\w*|ахрененн\w*", "fucking awesome"),
    (r"[оа]ху[еи]ть|[оа]хренеть|[оа]хуеваю|[оа]хереть", "holy shit"),
    (r"ху[её]во|херово", "дерьмово"),
    (r"ху[её]в(?:ый|ая|ое|ые|ого|ую|ой|ых)|херов(?:ый|ая|ое|ые|ого|ую|ой|ых)", "shitty"),
    (r"[её]бан(?:ут|ат|ь)\w*|[её]бнут\w*|долб[оа][её]б\w*|далб[оа][её]б\w*", "fucking idiot"),
    (r"у[её]б(?:ок|ка|ки|ку|ков|ище|ищ|ан)\w*", "fucking asshole"),
    (r"ху[еи]сос\w*", "cocksucker"),
    (r"г[ао]нд[ао]н\w*", "dickhead"),
    (r"мудак\w*|мудил\w*|мудозвон\w*", "asshole"),
    (r"(?:ебал|ебала)\s+(?:я|в\s+рот)|(?:я|в\s+рот)\s+ебал", "fuck"),
    (r"хуйн\w*|хуета|хуита", "bullshit"),
    (r"сука\s+бля\w*|бля\w*\s+сука", "fucking hell"),
    (r"бля+д?[ьъ]?|бля+т[ьъ]?|блэт|блеа+т", "fuck"),
    (r"ебать", "damn"), (r"[её]баны[йе]|[её]баная|[её]баное|[её]бучи[йе]|[её]бучая", "fucking"),
    (r"лох|лошара|лохушка", "loser"), (r"лохи|лошары", "losers"),
]
RU_MAT = [(re.compile(r"(?<![А-Яа-яЁё])(?:" + p + r")(?![А-Яа-яЁё])", re.I), r) for p, r in RU_MAT]
# правильный английский -> разговорный, как пишут в игровых чатах (для F9; Shift+F9 - без этого)
EN_CASUAL = [
    (r"\bI do(?:n't| not) know\b", "idk"), (r"\b(?:I'?ll|I will) be right back\b", "brb"),
    (r"\bbe right back\b", "brb"), (r"\b(?:I'?m|I am) on my way\b", "omw"), (r"\bon my way\b", "omw"),
    (r"\bby the way\b", "btw"), (r"\bto be honest\b", "tbh"), (r"\bin my opinion\b", "imo"),
    (r"\bright now\b", "rn"), (r"\blet me know\b", "lmk"), (r"\bnever mind\b", "nvm"), (r"\bof course\b", "ofc"),
    (r"\bthank you (?:so|very) much\b", "thx a lot"), (r"\bthank you\b", "ty"), (r"\bthanks\b", "thx"),
    (r"\byou'?re welcome\b", "np"), (r"\bno problem\b", "np"), (r"\bplease\b", "pls"), (r"\bokay\b", "ok"),
    (r"\bgoing to\b", "gonna"), (r"\bwant to\b", "wanna"), (r"\b(?:have|has) got to\b", "gotta"),
    (r"\bgot to\b", "gotta"), (r"\bkind of\b", "kinda"), (r"\bdon't know\b", "dunno"),
    (r"\bgood game\b", "gg"), (r"\bwell played\b", "wp"), (r"\bgood luck\b", "gl"), (r"\bhave fun\b", "hf"),
    (r"\boh my god\b", "omg"), (r"\bbecause\b", "cuz"), (r"\bnewbies\b", "noobs"), (r"\bnewbie\b", "noob"),
    (r"\bcome on\b", "c'mon"), (r"\bsee you\b", "cya"), (r"\bminutes\b", "mins"), (r"\bseconds\b", "secs"),
    (r"\bdude\b", "bro"), (r"\bguys\b", "guys"), (r"\bpeople\b", "ppl"), (r"\bsomething\b", "smth"),
]


def normalize_ru(text):
    """Русский сленг и растянутые буквы ("приииивет") -> то, что Google поймёт правильно."""
    text = re.sub(r"([а-яё])\1{2,}", r"\1", text, flags=re.I)
    for pattern, repl in RU_MAT:
        text = pattern.sub(repl, text)

    def word(m):
        w = m.group(0)
        r = RU_SLANG.get(w.lower())
        if r is None:
            return w
        return r[:1].upper() + r[1:] if w[:1].isupper() else r
    return re.sub(r"[А-Яа-яЁё]+", word, text)


def casual_en(text):
    """Делаем английский перевод разговорным, как в игровом чате."""
    for pattern, repl in EN_CASUAL:
        text = re.sub(pattern, repl, text, flags=re.I)
    # в чатах не пишут с большой буквы и не ставят точку в конце ("I" остаётся большой)
    text = re.sub(r"(^|[.!?]\s+)([A-Z])(?=[a-z])", lambda m: m.group(1) + m.group(2).lower(), text.strip())
    return re.sub(r"(?<![.])\.$", "", text)


def translate_to(text, lang, casual=False):
    """Перевод для F9 с любого языка на lang. Пробелы по краям сохраняем."""
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if lang == "en":
        key = f"ai->{lang}{'~' if casual else ''}:{text.strip()}"
        if key not in cache:
            try:
                cache[key] = ai_to_en(text.strip(), casual)
                save_cache()
            except AIUnavailable:
                pass
        if key in cache:
            last_source[0] = "нейросеть"
            return lead + cache[key] + trail
    last_source[0] = "Google"
    core = normalize_ru(text.strip())
    key = f"->{lang}:{core}"
    if key not in cache:
        _last_used[0] = time.time()
        # с опечатками Google может не узнать русский ("превет как дила") - подсказываем язык
        source = "ru" if CYRILLIC.search(core) else "auto"
        cache[key] = online_translate([core], source, lang, spell=True)[0].strip()
        save_cache()
    out = casual_en(cache[key]) if casual and lang == "en" else cache[key]
    return lead + out + trail


def keep_connection():
    """Открываем соединения сразу при запуске и не даём им закрыться, пока программой пользуются
    (редко - чтобы лишние запросы не злили Google)."""
    for warm in (lambda: _google("hi"), lambda: _yandex(["hi"], "en", "ru")):
        try:
            warm()
        except Exception:
            pass
    while True:
        time.sleep(90)
        if time.time() - _last_used[0] < 10 * 60:
            try:
                online_translate(["hi"])
            except Exception:
                pass


def load_cache():
    """Переводы прошлых запусков: меню, подсказки, частые фразы показываются сразу, без интернета."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_cache():
    with _cache_lock:
        if len(cache) > CACHE_LIMIT:
            for key in list(cache)[:len(cache) - CACHE_LIMIT]:   # самые старые
                del cache[key]
        try:
            with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
        except OSError:
            pass


def translate(texts):
    todo = [t for t in dict.fromkeys(texts) if t not in cache]
    if todo:
        _last_used[0] = time.time()
        cache.update(zip(todo, (s.strip() for s in online_translate(todo))))
        save_cache()
    return [cache[t] for t in texts]


def same(s):
    return re.sub(r"[\W_]+", "", s).lower()


def expand_slang(text):
    """Разговорные слова и сленг -> обычный английский, который Google переводит нормально.
    Возвращает (текст для перевода, готовый русский перевод или None)."""
    # распознаватель путает "o" и ноль: "0k" -> "ok", "n0" -> "no" (но "100", "m8" не трогаем)
    text = re.sub(r"(?<=[A-Za-z])0|0(?=[A-Za-z])", "o", text)
    key = re.sub(r"[^\w\s]", "", text).strip().lower()
    key = re.sub(r"\s+", " ", key)
    if key in SLANG_PHRASES:
        return None, SLANG_PHRASES[key]
    # растянутые слова: "pleeease" -> "please", "sooo" -> "so", "!!!!" -> "!"
    text = re.sub(r"([a-z])\1{2,}", r"\1", text)
    text = re.sub(r"([!?.])\1{2,}", r"\1", text)

    def word(m):
        w = m.group(0)
        e = SLANG_WORDS.get(w.lower())
        # одиночные буквы ("u", "r", "k") - только строчные: "Press R" это клавиша, а не "are"
        if e is None or (len(w) == 1 and not w.islower()):
            return w
        return e
    return re.sub(r"[A-Za-z0-9']+", word, text), None


def translate_blocks(blocks, ai=False):
    """Ник в начале сообщения ("[xXSniperXx]: ...") не переводим - только сам текст."""
    for b in blocks:
        m = CHAT_PREFIX.match(b["text"])
        b["prefix"] = m.group(0) if m else ""
        b["msg"] = b["text"][len(b["prefix"]):].strip()
        if b["prefix"].startswith("[") and "]" not in b["prefix"]:
            b["prefix"] = re.sub(r"\s*[1lI|]?:\s*$", "]: ", b["prefix"])  # "[Player1231:" -> "[Player123]:"
    blocks = [b for b in blocks if re.search(r"[A-Za-z]{2,}", b["msg"]) or b["msg"].lower() in SHORT_WORDS]
    ready = [expand_slang(b["msg"]) for b in blocks]
    todo = [src for src, ru in ready if ru is None]
    done = None
    if ai and todo:
        try:
            need = [t for t in dict.fromkeys(todo) if "ai>ru:" + t not in cache]
            if need:
                cache.update(("ai>ru:" + t, r) for t, r in zip(need, ai_to_ru(need)))
                save_cache()
            done = iter([cache["ai>ru:" + t] for t in todo])
            last_source[0] = "нейросеть"
        except (AIUnavailable, ValueError, KeyError, AttributeError):
            done = None
    if done is None:
        done = iter(translate(todo))
        last_source[0] = "Google"
    for b, (src, ru) in zip(blocks, ready):
        tr = ru if ru is not None else next(done)
        b["tr"] = b["prefix"] + tr
        # перевод совпал с оригиналом (имя, название) - переводить было нечего
        b["same"] = same(b["msg"]) == same(tr)
    return blocks


def split_to_lines(text, lines, measure):
    """Раскладываем перевод абзаца обратно по строкам оригинала - пропорционально их длине."""
    if len(lines) == 1:
        return [text]
    widths = [max(1, l[2] - l[0]) for l in lines]
    total = measure(text)
    targets, acc = [], 0
    for w in widths:
        acc += w
        targets.append(total * acc / sum(widths))
    parts = [[] for _ in lines]
    i, pos = 0, 0
    for word in text.split():
        ww = measure(word + " ")
        # слово больше чем наполовину вылезает за свою долю - переносим на следующую строку
        if i < len(lines) - 1 and pos + ww / 2 > targets[i] and parts[i]:
            i += 1
        parts[i].append(word)
        pos += ww
    return [" ".join(p) for p in parts]


def hexcolor(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c[:3])


def line_colors(img, box):
    """Цвет фона вокруг строки и цвет её букв - чтобы перевод выглядел как родной текст игры."""
    x1, y1, x2, y2 = box
    pad = 3
    crop = img.crop((max(0, x1 - pad), max(0, y1 - pad),
                     min(img.width, x2 + pad), min(img.height, y2 + pad))).convert("RGB")
    cw, ch = crop.size
    if cw < 2 * pad + 2 or ch < 2 * pad + 2:
        return CARD_COLOR, TEXT_COLOR
    px = crop.load()
    border = ([px[x, 0] for x in range(cw)] + [px[x, ch - 1] for x in range(cw)]
              + [px[0, y] for y in range(ch)] + [px[cw - 1, y] for y in range(ch)])
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    inner = list(crop.crop((pad, pad, cw - pad, ch - pad)).getdata())
    dist = lambda c: abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2])
    far = sorted(inner, key=dist)[-max(1, len(inner) // 12):]
    fg = tuple(sum(c[i] for c in far) / len(far) for i in range(3))
    if dist(fg) < 150:
        # буквы плохо отличаются от фона - берём белый или чёрный, что контрастнее
        light = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        fg = (20, 20, 20) if light > 140 else (245, 245, 245)
    return hexcolor(bg), hexcolor(fg)


def layout_block(b, measure, W, H):
    """Где и как рисовать перевод блока: каждая английская строка закрывается своей русской -
    ровно на её месте и её размера. Перевод длиннее - уменьшаем шрифт, а не растягиваем полоску.
    measure(текст, размер_шрифта) -> ширина. Возвращает [(полоска, точка_текста, текст, шрифт, фон, цвет)]."""
    lines = b["lines"]
    colors = b.get("colors") or [(CARD_COLOR, TEXT_COLOR)] * len(lines)
    heights = sorted(l[3] - l[1] for l in lines)
    lh = max(8, heights[len(heights) // 2])          # высота строки оригинала
    scale = settings["text_scale"]
    base_px = max(10, round(lh * 1.05 * scale))
    # мельче 80% оригинала не делаем - читать невозможно; лучше чуть удлинить полоску вправо
    min_px = max(9, round(lh * 0.8 * scale))
    parts = split_to_lines(b["tr"], lines, lambda s: measure(s, base_px))
    out = []
    for part, (x1, y1, x2, y2), (bg, fg) in zip(parts, lines, colors):
        w = max(8, x2 - x1)
        px = base_px
        tw = measure(part, px)
        while tw > w * 1.03 and px > min_px:
            px -= 1
            tw = measure(part, px)
        # полоска - ровно по строке оригинала (шире, только если перевод совсем не влез)
        mid = (y1 + y2) / 2
        half = max((y2 - y1) / 2, px * 0.62)      # крупный шрифт (настройка "размер текста") - полоска выше
        card = (max(0, x1 - 2), max(0, mid - half - 2), min(W, max(x2, x1 + tw) + 2), min(H, mid + half + 2))
        out.append((card, (x1, mid), part, px, bg, fg))
    return out


FONT_FILE = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
_pil_fonts = {}


def pil_font(px):
    if px not in _pil_fonts:
        _pil_fonts[px] = ImageFont.truetype(FONT_FILE, px)
    return _pil_fonts[px]


def render_translation(img, blocks):
    """Картинка для перевода области (F7): тот же перевод, нарисованный прямо на снимке."""
    out = img.convert("RGB")
    d = ImageDraw.Draw(out)
    for b in blocks:
        for card, (tx, ty), part, px, bg, fg in layout_block(
                b, lambda s, px: pil_font(px).getlength(s), out.width, out.height):
            d.rectangle(card, fill=bg)
            if part:
                d.text((tx, ty), part, font=pil_font(px), fill=fg, anchor="lm")
    return out


def copy_image(img):
    """Кладём картинку в буфер обмена - потом её можно вставить в Discord, Word, Paint (Ctrl+V)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]                        # без заголовка файла = формат CF_DIB
    return set_clipboard([(CF_DIB, data)])


# ---------- буфер обмена и нажатия клавиш (для F9) ----------
CF_DIB = 8
CF_UNICODETEXT = 13
# форматы, которые хранятся не как кусок памяти (картинки GDI и т.п.) - их не сохраняем
GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x81, 0x82, 0x83, 0x8E}
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
KEYEVENTF_KEYUP = 0x0002


def open_clipboard():
    for _ in range(50):          # буфер может на миг держать другая программа
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def set_clipboard(items):
    """Кладём в буфер обмена [(формат, байты), ...]."""
    if not open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        ok = True
        for fmt, data in items:
            handle = kernel32.GlobalAlloc(0x0002, max(1, len(data)))     # GMEM_MOVEABLE
            ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
            kernel32.GlobalUnlock(handle)
            ok = bool(user32.SetClipboardData(fmt, handle)) and ok
        return ok
    finally:
        user32.CloseClipboard()


def clipboard_snapshot():
    """Запоминаем всё, что лежит в буфере (текст, картинку...), чтобы потом вернуть как было."""
    items = []
    if not open_clipboard():
        return None
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in GDI_FORMATS:
                continue
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            size = kernel32.GlobalSize(handle)
            ptr = kernel32.GlobalLock(handle) if 0 < size <= 64 * 1024 * 1024 else None
            if not ptr:
                continue
            try:
                items.append((fmt, ctypes.string_at(ptr, size)))
            finally:
                kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return items


def get_clipboard_text():
    if not open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        ptr = kernel32.GlobalLock(handle) if handle else None
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    return set_clipboard([(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))])


def press_ctrl(key):
    """Нажать Ctrl+<клавиша>, как будто это сделал человек (со скан-кодами - некоторые программы
    не реагируют на нажатия без них)."""
    vk = ord(key)
    scan_ctrl, scan_key = user32.MapVirtualKeyW(VK_CONTROL, 0), user32.MapVirtualKeyW(vk, 0)
    user32.keybd_event(VK_CONTROL, scan_ctrl, 0, 0)
    user32.keybd_event(vk, scan_key, 0, 0)
    time.sleep(0.01)
    user32.keybd_event(vk, scan_key, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, scan_ctrl, KEYEVENTF_KEYUP, 0)


def wait_keys_released(timeout=1.5):
    """Ждём, пока отпустят F9 и Shift/Ctrl/Alt, иначе наш Ctrl+C смешается с ними."""
    end = time.time() + timeout
    while time.time() < end and any(user32.GetAsyncKeyState(k) & 0x8000
                                    for k in (VK_SHIFT, VK_CONTROL, VK_MENU, settings["key_type"])):
        time.sleep(0.01)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("flags", ctypes.wintypes.DWORD),
                ("hwndActive", ctypes.wintypes.HWND), ("hwndFocus", ctypes.wintypes.HWND),
                ("hwndCapture", ctypes.wintypes.HWND), ("hwndMenuOwner", ctypes.wintypes.HWND),
                ("hwndMoveSize", ctypes.wintypes.HWND), ("hwndCaret", ctypes.wintypes.HWND),
                ("rcCaret", ctypes.wintypes.RECT)]


WM_COPY, WM_PASTE = 0x0301, 0x0302
user32.SendMessageTimeoutW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM,
                                       ctypes.wintypes.LPARAM, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
type_lock = threading.Lock()     # одно F9 за раз: два одновременных Ctrl+C/Ctrl+V путают буфер


def edit_control(window):
    """Поле ввода в окне, где стоит каретка (обычное Edit или RichEdit, как в Блокноте), иначе None.
    Ему можно послать WM_COPY/WM_PASTE напрямую - это срабатывает, даже если клавиши уходят
    не туда (например, после нажатия Alt в окне активно меню)."""
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(user32.GetWindowThreadProcessId(window, None), ctypes.byref(info)):
        return None
    def is_edit(hwnd):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        return buf.value.lower() == "edit" or buf.value.lower().startswith("richedit")

    for hwnd in (info.hwndFocus, info.hwndCaret):
        if hwnd and is_edit(hwnd):
            return hwnd
    # фокус ушёл в меню/панель (в Блокноте Windows 11 так бывает после Alt) - если поле ввода
    # в окне одно, выделенный текст наверняка в нём
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)(
        lambda h, _: (found.append(h) if user32.IsWindowVisible(h) and is_edit(h) else None, True)[1])
    user32.EnumChildWindows(window, proc, 0)
    return found[0] if len(found) == 1 else None


def wait_clipboard_change(seq, timeout):
    end = time.time() + timeout
    while user32.GetClipboardSequenceNumber() == seq and time.time() < end:
        time.sleep(0.01)
    return user32.GetClipboardSequenceNumber() != seq


def copy_selection(window):
    """Копируем выделенный текст. Возвращает (текст или None, поле ввода для WM_PASTE или None)."""
    seq = user32.GetClipboardSequenceNumber()
    press_ctrl("C")
    direct = None
    if not wait_clipboard_change(seq, 0.8):
        # Ctrl+C не дошёл (фокус в меню, программа задумалась...) - пробуем напрямую и ещё раз клавишами
        direct = edit_control(window)
        if direct:
            user32.SendMessageTimeoutW(direct, WM_COPY, 0, 0, 0x2, 500, None)   # SMTO_ABORTIFHUNG
        if not wait_clipboard_change(seq, 0.3):
            direct = None
            wait_keys_released()
            press_ctrl("C")
            if not wait_clipboard_change(seq, 0.8):
                return None, None
    # программа могла только очистить буфер и ещё не положить текст - ждём текст
    end = time.time() + 0.5
    while True:
        src = get_clipboard_text()
        if src or time.time() > end:
            return src, direct
        time.sleep(0.02)


def type_translate(casual):
    """F9 в обычных программах: копируем выделенный текст, переводим на английский и вставляем
    на его место. Буфер обмена после этого возвращаем таким, каким он был.
    Если ничего не выделено - открываем плашку для набора текста (как в играх)."""
    if not type_lock.acquire(blocking=False):
        return                   # предыдущее F9 ещё не закончилось
    try:
        wait_keys_released()
        window = user32.GetForegroundWindow()   # куда потом вставлять перевод
        saved = clipboard_snapshot()
        src, direct = copy_selection(window)
        if not src or not src.strip():
            if saved is not None and src is not None:
                set_clipboard(saved)
            events.put(("cap_start", window, casual))
            return
        result = translate_to(src, TYPE_LANG, casual)
        set_clipboard_text(result)
        if user32.GetForegroundWindow() != window:
            # пока переводили, переключились в другое окно - не вставляем вслепую куда попало
            events.put(("type_done", "Перевод в буфере обмена — вставьте его Ctrl+V", "warn"))
            return
        time.sleep(0.03)
        if direct:
            user32.SendMessageTimeoutW(direct, WM_PASTE, 0, 0, 0x2, 1000, None)
        else:
            press_ctrl("V")
        time.sleep(0.4)          # программа должна успеть вставить, прежде чем вернём буфер
        if saved is not None:
            set_clipboard(saved)
        events.put(("type_done", "Заменено английским переводом", "info"))
    except Exception as e:
        events.put(("type_done", f"Ошибка: {e}", "warn"))
    finally:
        type_lock.release()


# ---------- F9 в играх: набор текста в плашку и "впечатывание" перевода ----------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD), ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.wintypes.DWORD), ("scanCode", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.ToUnicodeEx.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_wchar_p,
                               ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
user32.VkKeyScanExW.restype = ctypes.c_short
user32.VkKeyScanExW.argtypes = [ctypes.c_wchar, ctypes.c_void_p]
user32.MapVirtualKeyExW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

KLF_NOTELLSHELL = 0x80
HKL_EN = user32.LoadKeyboardLayoutW("00000409", KLF_NOTELLSHELL)
HKL_RU = user32.LoadKeyboardLayoutW("00000419", KLF_NOTELLSHELL)
WM_INPUTLANGCHANGEREQUEST = 0x0050
KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0004, 0x0008
TYPE_KEY_DELAY = 0.012   # пауза между нажатиями: игры читают клавиатуру раз в кадр и пропускают слишком быстрые


def is_fullscreen(hwnd):
    """Окно на весь монитор - скорее всего, игра (там Ctrl+C/Ctrl+V обычно не работают)."""
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
        return False
    m = info.rcMonitor
    if user32.GetWindowLongW(hwnd, -16) & 0x00C00000 == 0x00C00000:   # WS_CAPTION
        return False         # у окна есть заголовок - обычная программа, хоть и развёрнутая
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


def send_key(scan=0, vk=0, flags=0):
    inp = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def type_text(text, hwnd):
    """Впечатываем текст в игру, как будто его набирают на клавиатуре. Английские буквы - настоящими
    нажатиями клавиш (их понимают почти все игры), остальное - символами Юникода."""
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    old_layout = user32.GetKeyboardLayout(tid)
    english = (old_layout or 0) & 0xFFFF == 0x0409
    if not english:
        # нажатие клавиши "H" при русской раскладке даст "Р" - на время переключаем окно на английскую
        user32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, HKL_EN)
        for _ in range(20):
            time.sleep(0.01)
            if (user32.GetKeyboardLayout(tid) or 0) & 0xFFFF == 0x0409:
                english = True
                break
    for ch in text.replace("\r", "").replace("\n", " "):
        code = user32.VkKeyScanExW(ch, HKL_EN) if english and " " <= ch <= "~" else -1
        if code != -1 and code & 0xFF != 0xFF:
            vk, shift = code & 0xFF, code & 0x100
            scan = user32.MapVirtualKeyExW(vk, 0, HKL_EN)
            if shift:
                send_key(0x2A, flags=KEYEVENTF_SCANCODE)
            send_key(scan, flags=KEYEVENTF_SCANCODE)
            time.sleep(TYPE_KEY_DELAY / 2)
            send_key(scan, flags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
            if shift:
                send_key(0x2A, flags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
        else:
            send_key(ord(ch), flags=KEYEVENTF_UNICODE)
            send_key(ord(ch), flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        time.sleep(TYPE_KEY_DELAY)
    if old_layout and (old_layout & 0xFFFF) != 0x0409:
        user32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, old_layout)   # возвращаем раскладку


def key_to_char(vk, scan, shift, russian):
    state = (ctypes.c_ubyte * 256)()
    if shift:
        state[VK_SHIFT] = 0x80
    if user32.GetKeyState(0x14) & 1:      # Caps Lock
        state[0x14] = 1
    buf = ctypes.create_unicode_buffer(8)
    n = user32.ToUnicodeEx(vk, scan, state, buf, 8, 0x4, HKL_RU if russian else HKL_EN)
    return buf.value[:n] if n > 0 else ""


class KeyCapture:
    """Пока открыта плашка F9, нажатия клавиш перехватываются и не попадают в игру: текст
    набирается в плашке, а игра не теряет фокус (иначе многие игры закрывают чат)."""

    def __init__(self):
        self.text = ""
        self.russian = True
        self.shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        self.ctrl = False
        self.f9_released = False   # F9, которым открыли плашку, ещё может быть зажат
        self.thread_id = None
        self.proc = HOOKPROC(self._hook)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(13, self.proc, kernel32.GetModuleHandleW(None), 0)   # WH_KEYBOARD_LL
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)   # WM_QUIT

    def _hook(self, code, wparam, lparam):
        try:
            if code != 0:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            k = KBDLLHOOKSTRUCT.from_address(lparam)
            if k.flags & 0x10:                  # нажатие сделала программа (наше впечатывание) - пропускаем
                return user32.CallNextHookEx(None, code, wparam, lparam)
            down = wparam in (0x0100, 0x0104)
            vk = k.vkCode
            if vk in (0x10, 0xA0, 0xA1):
                self.shift = down
                return 1
            if vk in (0x11, 0xA2, 0xA3):
                self.ctrl = down
                return 1
            if vk in (0x12, 0xA4, 0xA5, 0x5B, 0x5C):   # Alt и Win пропускаем (Alt+Tab должен работать)
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if vk == settings["key_type"] and not down:
                self.f9_released = True
            if not down:
                return 1
            if vk == 0x0D:                                       # Enter - перевести и вписать
                events.put(("cap_commit", self.shift))
            elif vk == 0x1B or (vk == settings["key_type"] and self.f9_released):   # Esc / F9 - отмена
                events.put(("cap_cancel",))
            elif vk == 0x08:                                     # Backspace (Ctrl - целое слово)
                self.text = re.sub(r"\S*\s*$", "", self.text) if self.ctrl else self.text[:-1]
                events.put(("cap_update",))
            elif vk == 0x09:                                     # Tab - раскладка RU/EN
                self.russian = not self.russian
                events.put(("cap_update",))
            elif self.ctrl and vk == ord("V"):
                events.put(("cap_paste",))
            elif self.ctrl and vk == ord("C"):                  # Ctrl+C - перевод в буфер обмена
                events.put(("cap_copy", self.shift))
            elif not self.ctrl:
                ch = key_to_char(vk, k.scanCode, self.shift, self.russian)
                if ch and ch.isprintable():
                    self.text += ch
                    events.put(("cap_update",))
            return 1
        except Exception:
            return user32.CallNextHookEx(None, code, wparam, lparam)


def copy_translation(text, casual):
    """Ctrl+C в плашке: английский перевод - в буфер обмена, чтобы вставить куда угодно."""
    try:
        set_clipboard_text(translate_to(text, TYPE_LANG, casual))
        events.put(("type_done", "Перевод скопирован — вставьте Ctrl+V", "info"))
    except Exception as e:
        events.put(("type_done", f"Ошибка: {e}", "warn"))


def capture_output(text, casual, hwnd):
    """Плашка закрыта по Enter: переводим и впечатываем английский текст туда, где писали."""
    try:
        result = translate_to(text, TYPE_LANG, casual)
        if user32.GetForegroundWindow() != hwnd:
            set_clipboard_text(result)
            events.put(("type_done", "Окно сменилось — перевод в буфере, вставьте Ctrl+V", "warn"))
            return
        type_text(result, hwnd)
    except Exception as e:
        events.put(("type_done", f"Ошибка: {e}", "warn"))


def process(img, ai=False):
    """Распознать и перевести снимок. Возвращает блоки с переводом и цветами строк."""
    _, blocks = analyze(img)
    blocks = [b for b in translate_blocks(blocks, ai) if not b["same"]]
    for b in blocks:
        b["colors"] = [line_colors(img, l) for l in b["lines"]]
    return blocks


def shot_work(mon, region, img):
    try:
        blocks = process(img, ai=True)
        # перевод текстом - сверху вниз, слева направо, как читают
        text = "\n".join(b["tr"] for b in sorted(blocks, key=lambda b: (b["box"][1], b["box"][0])))
        events.put(("shot_ready", mon, region, img, render_translation(img, blocks), len(blocks), text))
    except Exception as e:
        events.put(("status", f"Ошибка: {e}"))


def work(gen, img, ai=False):
    try:
        events.put(("result", gen, process(img, ai)))
    except Exception as e:
        events.put(("fail", gen, f"Ошибка: {e}"))


def report(img):
    """Shift+F8: сохраняет снимок экрана и список строк - что перевелось, что нет и почему."""
    try:
        folder = os.path.join(APP_DIR, "отчёты")
        os.makedirs(folder, exist_ok=True)
        name = time.strftime("%Y-%m-%d_%H-%M-%S")
        img.save(os.path.join(folder, name + ".png"))
        segs, blocks = analyze(img)
        blocks = translate_blocks(blocks)
        with open(os.path.join(folder, name + ".txt"), "w", encoding="utf-8") as f:
            f.write("=== ПЕРЕВОД (блоками) ===\n")
            for b in blocks:
                mark = "совпал с оригиналом, не показан" if b["same"] else "показан"
                f.write(f"{tuple(b['box'])}  [{mark}]\n    {b['text']}\n    -> {b['tr']}\n")
            f.write("\n=== ПРОПУЩЕНО ===\n")
            for s in segs:
                if s["skip"]:
                    f.write(f"{s['box']}  ({s['skip']})\n    {s['text']}\n")
        events.put(("status", f"Отчёт сохранён: отчёты\\{name}"))
    except Exception as e:
        events.put(("status", f"Ошибка отчёта: {e}"))


# ---------- окно поверх игры ----------
events_state = {"chat": False}     # для галочки "Переводить чат" в меню значка


def tray_title():
    return (f"Переводчик\n{key_name(settings['key_screen'])} — экран, {key_name(settings['key_region'])} — "
            f"область, {key_name(settings['key_type'])} — написать")


def tray_thread():
    """Значок в трее: меню с теми же действиями, что на клавишах, настройки и выход."""
    try:
        import pystray
    except ImportError:
        return None
    try:
        image = Image.open(resource("icon.ico"))
    except OSError:
        image = Image.new("RGB", (64, 64), ACCENT_COLOR)
    item, menu = pystray.MenuItem, pystray.Menu

    def toggle(name):
        settings[name] = not settings[name]
        save_settings()
        events.put(("setting", name))

    def open_folder(name):
        folder = os.path.join(APP_DIR, name)
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def toggle_autostart():
        try:
            set_autostart(autostart_value() is None)
            events.put(("status", "Переводчик будет запускаться вместе с Windows" if autostart_value()
                        else "Автозапуск выключен"))
        except OSError as e:
            events.put(("status", f"Не удалось изменить автозапуск: {e}"))

    k = lambda name, mod="": lambda i: f"\t{mod}{key_name(settings[name])}"
    icon = pystray.Icon("GameTranslator", image, tray_title(), menu(
        item(lambda i: "Перевести весь экран" + k("key_screen")(i), lambda: events.put(("hotkey",)), default=True),
        item(lambda i: "Перевести область" + k("key_region")(i), lambda: events.put(("select",))),
        item(lambda i: "Переводить чат" + k("key_region", "Shift+")(i), lambda: events.put(("chat",)),
             checked=lambda i: events_state["chat"]),
        item(lambda i: "Выбрать область чата заново" + k("key_region", "Ctrl+")(i),
             lambda: events.put(("chat_new",))),
        menu.SEPARATOR,
        item("Переводить нейросетью (по смыслу)", lambda: toggle("ai"), checked=lambda i: settings["ai"]),
        item("Сленг в F9 (Shift+F9 — наоборот)", lambda: toggle("slang"), checked=lambda i: settings["slang"]),
        menu.SEPARATOR,
        item("Открыть папку со скринами", lambda: open_folder("скрины")),
        item("Открыть папку с отчётами", lambda: open_folder("отчёты")),
        item("Клавиши", lambda: events.put(("help",))),
        item("Запускать вместе с Windows", toggle_autostart, checked=lambda i: autostart_value() is not None),
        item("Настройки…", lambda: events.put(("settings",))),
        item("Перезапустить от имени администратора", restart_as_admin, visible=not is_admin()),
        menu.SEPARATOR,
        item(lambda i: "Выход" + k("key_screen", "Ctrl+")(i), lambda: events.put(("quit",))),
    ))
    icon.run_detached()
    return icon


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.live = False
        self.gen = 0             # номер сеанса: ответы от старых сеансов игнорируем
        self.working = False
        self.overlay = None
        self.canvas = None
        self.mon = None
        self.area = None         # что снимаем в живом режиме: весь монитор (F8) или область чата
        self.chat = None         # область чата (x1, y1, x2, y2) внутри монитора, если переводим чат
        self.pill_top = True
        self.draw_id = 0         # номер отрисовки: отложенное "спрятать" срабатывает, только если новой не было
        self.settings_win = None
        self.selector = None
        self.prev = None
        self.hidden_ok = False
        self.shown = None        # что сейчас нарисовано
        self.calm = 0            # сколько проверок подряд текст не менялся
        self.toast = None
        self.tray = None
        self.fonts = {}
        self.shot_win = None     # окно с переводом выделенной области (F7)
        self.cap = None          # плашка F9 для набора текста
        self.cap_win = self.cap_canvas = self.cap_job = None
        self.cap_tr = ""
        self.cap_src = ""
        self.cap_pending = False
        load_settings()
        load_cache()
        # вторая копия программы (ярлык нажали ещё раз) - клавиши и значок не трогаем, только сообщение
        self._mutex = kernel32.CreateMutexW(None, False, "GameTranslator_single_instance")
        self.duplicate = kernel32.GetLastError() == 183      # ERROR_ALREADY_EXISTS
        # перезапуск от администратора: старая копия закрывается не мгновенно - ждём её до 5 секунд
        for _ in range(25 if "--restarted" in sys.argv else 0):
            if not self.duplicate:
                break
            kernel32.CloseHandle(self._mutex)
            time.sleep(0.2)
            self._mutex = kernel32.CreateMutexW(None, False, "GameTranslator_single_instance")
            self.duplicate = kernel32.GetLastError() == 183
        if not self.duplicate:
            threading.Thread(target=keep_connection, daemon=True).start()
            start_hotkeys(first=True)
            if autostart_value() not in (None, autostart_command()):
                try:
                    set_autostart(True)      # программу переложили в другую папку - обновляем путь
                except OSError:
                    pass
        self.root.after(50, self.poll)

    def poll(self):
        try:
            while True:
                self.handle(events.get_nowait())
        except queue.Empty:
            pass
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.ticks % 10 == 0:          # раз в полсекунды - свои окна снова наверх
            for win in (self.overlay, self.cap_win, self.toast, self.shot_win, self.selector):
                if win is not None:
                    raise_topmost(win)
        self.root.after(50, self.poll)

    def handle(self, ev):
        kind = ev[0]
        if kind == "hotkey":
            if self.selector:
                self.close_selector()
            elif self.live and not self.chat:
                self.stop()
            else:
                self.stop()              # перевод чата сменяется переводом всего экрана
                self.start()
        elif kind in ("chat", "chat_new"):
            saved = settings["chat_region"]
            if self.selector:
                self.close_selector()
            elif kind == "chat" and self.live and self.chat:
                self.stop()
                self.show_toast("Перевод чата на паузе", "info", ms=1800,
                                sub=f"Область запомнена — Shift+{key_name(settings['key_region'])} включит снова")
            elif kind == "chat" and saved:
                self.stop()
                self.start_chat(saved)
            else:
                self.stop()
                self.select(chat=True)
        elif kind == "select":
            if self.selector:
                self.close_selector()
            elif self.shot_win:
                self.close_shot()        # F7 при открытом переводе области - закрыть
            else:
                self.select()
        elif kind == "type":
            if not self.cap:
                casual = settings["slang"] if ev[1] else not settings["slang"]   # Shift+F9 - наоборот
                window = user32.GetForegroundWindow()
                if is_fullscreen(window):
                    self.cap_start(window, casual)     # игра: сразу плашка, Ctrl+C там не работает
                else:
                    threading.Thread(target=type_translate, args=(casual,), daemon=True).start()
        elif kind == "cap_start":
            if not self.cap:
                self.cap_start(ev[1], ev[2])
        elif kind == "cap_update" and self.cap:
            self.cap_pending = True
            self.cap_draw()
            if self.cap_job:
                self.root.after_cancel(self.cap_job)
            self.cap_job = self.root.after(400, self.cap_preview)
        elif kind == "cap_paste" and self.cap:
            self.cap.text += (get_clipboard_text() or "").replace("\r", "").replace("\n", " ")
            events.put(("cap_update",))
        elif kind == "cap_preview" and self.cap:
            if ev[1] == self.cap.text:
                self.cap_tr, self.cap_src, self.cap_pending = ev[2], ev[3], False
                self.cap_draw()
        elif kind == "cap_commit" and self.cap:
            text, casual, window = self.cap.text, self.cap_casual != bool(ev[1]), self.cap_window
            self.cap_close()
            if text.strip():
                threading.Thread(target=capture_output, args=(text, casual, window), daemon=True).start()
        elif kind == "cap_copy" and self.cap:
            text, casual = self.cap.text, self.cap_casual != bool(ev[1])
            self.cap_close()
            if text.strip():
                threading.Thread(target=copy_translation, args=(text, casual), daemon=True).start()
        elif kind == "cap_cancel" and self.cap:
            self.cap_close()
        elif kind == "type_done":
            self.show_toast(ev[1], ev[2], ms=1500)
        elif kind == "shot_ready":
            self.hide_toast()
            self.show_shot(*ev[1:])
        elif kind == "result":
            self.working = False
            if ev[1] == self.gen and self.live:
                self.hide_toast()
                # тот же текст на тех же местах - не перерисовываем, чтобы перевод не мигал
                sig = tuple((b["tr"], *(v // 8 for v in b["box"])) for b in ev[2])
                if sig == self.shown:
                    self.calm = min(self.calm + 1, 4)
                else:
                    self.calm = 0
                    self.shown = sig
                    self.draw(ev[2])
        elif kind == "fail":
            self.working = False
            if ev[1] == self.gen and self.live:
                self.prev = None  # попробуем ещё раз
                self.show_toast(ev[2], "warn", ms=2500)
        elif kind == "report":
            self.show_toast("Сохраняю отчёт", "spin")
            img = grab(self.mon if self.live else cursor_monitor())
            threading.Thread(target=report, args=(img,), daemon=True).start()
        elif kind == "status":
            self.show_toast(ev[1], "info", ms=3500)
        elif kind == "error":
            print(ev[1])
            self.show_toast(ev[1], "error", ms=6000)
            self.root.after(6000, self.root.destroy)
        elif kind == "setting":
            on = settings[ev[1]]
            if ev[1] == "ai":
                _ai_off_until[0] = 0
                self.show_toast("Нейросеть включена — F9 и F7 переводят по смыслу" if on else
                                "Нейросеть выключена — переводит Google", "info", ms=2500)
            else:
                self.show_toast("F9 пишет со сленгом, Shift+F9 — без" if on else
                                "F9 пишет без сленга, Shift+F9 — со сленгом", "info", ms=2500)
        elif kind == "help":
            self.show_help(ms=6000)
        elif kind == "settings":
            self.open_settings()
        elif kind == "quit_now":
            if self.tray:
                self.tray.stop()
            self.stop()
            kernel32.CloseHandle(self._mutex)     # чтобы новая копия (от администратора) не решила, что эта ещё жива
            self.root.destroy()
        elif kind == "quit":
            if self.tray:
                self.tray.stop()
            self.stop()
            self.show_toast("Переводчик закрыт", "info", ms=900)
            self.root.after(900, self.root.destroy)

    # ---- живой режим ----
    def start_chat(self, box):
        """Перевод чата: box - сохранённая область в координатах экрана."""
        mon = monitor_at((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        region = (max(0, box[0] - mon["left"]), max(0, box[1] - mon["top"]),
                  min(mon["width"], box[2] - mon["left"]), min(mon["height"], box[3] - mon["top"]))
        self.start(mon, region)

    def start(self, mon=None, region=None):
        self.live = True
        self.gen += 1
        self.prev = None
        self.shown = None
        self.calm = 0
        self.mon = mon or cursor_monitor()
        self.chat = region
        events_state["chat"] = bool(region)
        if region:
            # окно перевода - ровно над чатом, плюс место под строку состояния (над чатом или под ним)
            x1, y1, x2, y2 = region
            self.area = {"left": self.mon["left"] + x1, "top": self.mon["top"] + y1,
                         "width": x2 - x1, "height": y2 - y1}
            self.pill_top = y1 >= 40
            top = self.mon["top"] + y1 - (40 if self.pill_top else 0)
            self.overlay, self.canvas = self.layer(max(x2 - x1, 360), y2 - y1 + 40, self.mon["left"] + x1, top)
        else:
            self.area = self.mon
            W, H = self.mon["width"], self.mon["height"]
            self.overlay, self.canvas = self.layer(W, H, self.mon["left"], self.mon["top"])
        self.hidden_ok = exclude_from_capture(self.overlay)
        self.draw([], updating=True)
        self.show_toast("Перевожу", "spin", mon=self.mon)
        self.root.after(30, self.tick, self.gen)

    # ---- выделение области мышкой ----
    def select(self, chat=False):
        """F7: обвести мышкой область - на её месте появится перевод.
        chat=True (Shift+F7): обвести окно чата - область запомнится и будет переводиться постоянно."""
        mon = cursor_monitor()
        W, H = mon["width"], mon["height"]
        game = user32.GetForegroundWindow()   # вернём фокус игре после выделения
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.35)
        win.geometry(f"{W}x{H}+{mon['left']}+{mon['top']}")
        c = tk.Canvas(win, width=W, height=H, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        c.create_text(W / 2, 70, text="Обведите мышкой окно чата" if chat else "Обведите мышкой текст для перевода",
                      fill="white", font=(FONT_BOLD, -26))
        c.create_text(W / 2, 108, fill="#C9CED8", font=(FONT, -16),
                      text="Новые сообщения будут переводиться сами, область запомнится. Esc — отмена" if chat
                      else f"Esc или {key_name(settings['key_region'])} — отмена")
        st = {}

        def press(e):
            st["x0"], st["y0"] = e.x, e.y
            st["rect"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT_COLOR, width=3, fill="white")

        def drag(e):
            if "rect" in st:
                c.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)

        def release(e):
            if "rect" not in st:
                return
            x1, x2 = sorted((st["x0"], e.x))
            y1, y2 = sorted((st["y0"], e.y))
            self.close_selector()
            user32.SetForegroundWindow(game)
            if x2 - x1 >= 15 and y2 - y1 >= 10:
                region = (max(0, x1), max(0, y1), min(W, x2), min(H, y2))
                # затемнение спрятано от снимков экрана - снимаем сразу, без паузы.
                # (если Windows не умеет прятать - ждём, пока затемнение исчезнет с экрана)
                if chat:
                    settings["chat_region"] = [mon["left"] + region[0], mon["top"] + region[1],
                                               mon["left"] + region[2], mon["top"] + region[3]]
                    save_settings()
                    self.root.after(0 if hidden else 120, self.start_chat, settings["chat_region"])
                else:
                    self.root.after(0 if hidden else 120, self.take_shot, mon, region)

        c.bind("<ButtonPress-1>", press)
        c.bind("<B1-Motion>", drag)
        c.bind("<ButtonRelease-1>", release)
        win.bind("<Escape>", lambda e: (self.close_selector(), user32.SetForegroundWindow(game)))
        win.focus_force()
        hidden = exclude_from_capture(win)
        self.selector = win

    def close_selector(self):
        if self.selector:
            self.selector.destroy()
            self.selector = None

    # ---- перевод выделенной области (F7) ----
    def take_shot(self, mon, region):
        img = grab(mon).crop(region)
        self.show_toast("Перевожу", "spin", mon=mon)
        threading.Thread(target=shot_work, args=(mon, region, img), daemon=True).start()

    def show_shot(self, mon, region, original, translated, count, text):
        """Переведённая картинка - ровно на месте выделенной области. Её можно перетаскивать."""
        self.close_shot()
        x1, y1, x2, y2 = region
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER_COLOR)
        photos = {"tr": ImageTk.PhotoImage(translated), "orig": ImageTk.PhotoImage(original)}
        state = {"view": "tr"}
        pic = tk.Label(win, image=photos["tr"], bd=0, cursor="fleur")
        bar = tk.Frame(win, bg=CARD_COLOR)

        def button(text, cmd, side="left", fg=TEXT_COLOR, hover="#26304A"):
            b = tk.Label(bar, text=text, bg=CARD_COLOR, fg=fg, font=(FONT, -13), padx=11, pady=6,
                         cursor="hand2")
            b.bind("<Button-1>", lambda e: cmd())
            b.bind("<Enter>", lambda e: b.configure(bg=hover))
            b.bind("<Leave>", lambda e: b.configure(bg=CARD_COLOR))
            b.pack(side=side)
            return b

        def copy():
            ok = copy_image(translated if state["view"] == "tr" else original)
            self.show_toast("Скопировано — вставьте Ctrl+V" if ok else "Не удалось скопировать",
                            "info" if ok else "warn", ms=2000, mon=mon)

        def copy_text():
            if not text:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.show_toast("Текст перевода скопирован — вставьте Ctrl+V", "info", ms=2000, mon=mon)

        def save():
            folder = os.path.join(APP_DIR, "скрины")
            os.makedirs(folder, exist_ok=True)
            name = time.strftime("%Y-%m-%d_%H-%M-%S") + ".png"
            translated.save(os.path.join(folder, name))
            self.show_toast(f"Сохранено: скрины\\{name}", "info", ms=2500, mon=mon)

        def toggle():
            state["view"] = "orig" if state["view"] == "tr" else "tr"
            pic.configure(image=photos[state["view"]])
            orig_btn.configure(text="Перевод" if state["view"] == "orig" else "Оригинал")

        button("Копировать картинку", copy)
        button("Копировать текст", copy_text)
        button("Сохранить", save)
        orig_btn = button("Оригинал", toggle)
        button("✕", self.close_shot, side="right", fg="#AEB6C6", hover="#5A2630")
        src = last_source[0]
        tk.Label(bar, text=(f"{count} стр. · {src}" if count else "английского текста нет"),
                 bg=CARD_COLOR, fg="#3DDC97" if count and src == "нейросеть" else "#8E97A8",
                 font=(FONT, -12), padx=8).pack(side="right")

        # панель кнопок - под картинкой, а если внизу не хватает места - над ней
        bar_h = 32
        below = y2 + bar_h + 4 <= mon["height"]
        if below:
            pic.pack(padx=1, pady=(1, 0))
            bar.pack(fill="x", padx=1, pady=(0, 1))
            top = y1 - 1
        else:
            bar.pack(fill="x", padx=1, pady=(1, 0))
            pic.pack(padx=1, pady=(0, 1))
            top = y1 - bar_h - 1
        win.geometry(f"+{mon['left'] + x1 - 1}+{mon['top'] + max(0, top)}")

        # перетаскивание мышкой
        drag = {}
        pic.bind("<ButtonPress-1>", lambda e: drag.update(x=e.x_root - win.winfo_x(), y=e.y_root - win.winfo_y()))
        pic.bind("<B1-Motion>", lambda e: win.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}"))
        win.bind("<Escape>", lambda e: self.close_shot())
        win.focus_force()
        win.photos = photos          # иначе картинки удалит сборщик мусора
        self.shot_win = win

    def close_shot(self):
        if self.shot_win:
            self.shot_win.destroy()
            self.shot_win = None

    def stop(self):
        self.live = False
        self.chat = None
        events_state["chat"] = False
        self.gen += 1
        if self.overlay:
            self.overlay.destroy()
            self.overlay = self.canvas = None
        self.hide_toast()

    def tick(self, gen):
        # у каждого сеанса своя цепочка проверок: старая цепочка после F8-F8 сама затухает
        if gen != self.gen or not self.live:
            return
        if self.working:
            self.root.after(200, self.tick, gen)
            return
        if self.hidden_ok:
            img = grab(self.area)
        else:
            # старая Windows не умеет прятать окно от снимка - на миг убираем его сами
            self.overlay.withdraw()
            self.root.update()
            img = grab(self.area)
            self.overlay.deiconify()
        small = img.convert("L").resize((320, 180))
        diff = changed_pixels(self.prev, small)
        if diff > 3:
            if diff > 320 * 180 * 0.3 and self.prev is not None and self.shown:
                # картинка сменилась почти целиком (другая вкладка, другое меню) - старый
                # перевод уже не на своём месте, убираем его сразу
                self.draw([], updating=True)
                self.shown = None
                self.calm = 0
            self.prev = small
            self.working = True
            threading.Thread(target=work, args=(gen, img, self.chat is not None), daemon=True).start()
        # если экран "шевелится" (анимация), а текст тот же - проверяем реже, бережём процессор
        self.root.after(int(LIVE_INTERVAL * 1000 * (1 + self.calm * 0.5)), self.tick, gen)

    # ---- общие штуки для рисования ----
    @staticmethod
    def rounded(c, x1, y1, x2, y2, r, **kw):
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    def layer(self, w, h, x, y, alpha=None):
        alpha = settings["opacity"] if alpha is None else alpha
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSPARENT)
        win.attributes("-transparentcolor", TRANSPARENT)
        win.attributes("-alpha", alpha)
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = tk.Canvas(win, width=w, height=h, bg=TRANSPARENT, highlightthickness=0)
        c.pack(fill="both", expand=True)
        make_clickthrough(win)
        return win, c

    # ---- перевод поверх игры ----
    def draw(self, blocks, updating=False):
        c = self.canvas
        c.delete("all")
        # поверх игры ничего не висит постоянно: строка состояния - 3 секунды, перевод чата - пока
        # идут сообщения (настройка "Прятать перевод чата"); новое сообщение - всё появляется снова
        self.draw_id += 1
        did = self.draw_id
        later = lambda ms, tag: self.root.after(ms, lambda: did == self.draw_id and self.canvas is c and c.delete(tag))
        if not updating:
            later(3000, "pill")
            if self.chat and blocks and settings["chat_hide"]:
                later(settings["chat_hide"] * 1000, "all")
        if self.chat:
            x1, y1, x2, y2 = self.chat
            W, H = x2 - x1, y2 - y1
            oy = 40 if self.pill_top else 0
            for b in blocks:
                self.draw_block(c, b, W, H, oy)
            status = "обновляю…" if updating else f"чат: {len(blocks)}" if blocks else "чат: жду сообщений"
            self.pill(c, 0, 2 if self.pill_top else H + 4, status, updating,
                      [("Shift+" + key_name(settings["key_region"]), "пауза")])
            return
        W, H = self.mon["width"], self.mon["height"]
        for b in blocks:
            self.draw_block(c, b, W, H)

        # подсказка внизу по центру
        if updating:
            status = "обновляю…"
        elif blocks:
            status = f"переведено: {len(blocks)}"
        else:
            status = "английского текста нет"
        keys = [(key_name(settings["key_screen"]), "выключить")]
        self.pill(c, (W - self.pill_width(status, keys)) / 2, H - 58, status, updating, keys)

    def pill_width(self, status, keys):
        return 30 + self.measure_font(status, (FONT, -14)) + 22 + self.keys_width(keys) + 14

    def pill(self, c, x, y, status, updating, keys):
        """Строка состояния: точка (зелёная - работает, жёлтая - обновляет), текст, клавиши."""
        hw = self.pill_width(status, keys)
        before = set(c.find_all())
        self.rounded(c, x, y, x + hw, y + 36, 18, fill="#0F131B", outline=BORDER_COLOR, width=1)
        c.create_oval(x + 14, y + 14, x + 22, y + 22, fill="#FFB547" if updating else "#3DDC97", outline="")
        c.create_text(x + 30, y + 18, text=status, font=(FONT, -14), fill="#C9CED8", anchor="w")
        self.draw_keys(c, x + 30 + self.measure_font(status, (FONT, -14)) + 22, y + 18, keys)
        for item in set(c.find_all()) - before:
            c.addtag_withtag("pill", item)

    def draw_block(self, c, b, W, H, oy=0):
        for card, (tx, ty), part, px, bg, fg in layout_block(b, self.measure, W, H):
            c.create_rectangle(card[0], card[1] + oy, card[2], card[3] + oy, fill=bg, outline="")
            if part:
                c.create_text(tx, ty + oy, text=part, font=(FONT, -px), fill=fg, anchor="w")

    def measure(self, text, px):
        if px not in self.fonts:
            self.fonts[px] = tkfont.Font(family=FONT, size=-px)
        return self.fonts[px].measure(text)

    # ---- плашка F9: пишешь по-русски - видишь английский ----
    def cap_start(self, window, casual):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(user32.MonitorFromWindow(window, 2), ctypes.byref(info)):
            m = info.rcMonitor
            mon = {"left": m.left, "top": m.top, "width": m.right - m.left, "height": m.bottom - m.top}
        else:
            mon = cursor_monitor()
        self.cap = KeyCapture()
        self.cap_window, self.cap_casual, self.cap_tr = window, casual, ""
        w, h = min(900, mon["width"] - 40), 136
        x = mon["left"] + (mon["width"] - w) // 2
        y = mon["top"] + mon["height"] - h - 120
        # окно не забирает фокус у игры (иначе чат закроется) и не видно на снимках экрана
        self.cap_win, self.cap_canvas = self.layer(w, h, x, y, alpha=0.97)
        exclude_from_capture(self.cap_win)
        self.cap.start()
        self.cap_draw()

    def cap_close(self):
        if self.cap:
            self.cap.stop()
            self.cap = None
        if self.cap_job:
            self.root.after_cancel(self.cap_job)
            self.cap_job = None
        if self.cap_win:
            self.cap_win.destroy()
            self.cap_win = self.cap_canvas = None

    def cap_preview(self):
        self.cap_job = None
        text, casual = self.cap.text, self.cap_casual

        def run():
            try:
                tr = translate_to(text, TYPE_LANG, casual) if text.strip() else ""
            except Exception as e:
                tr = f"(нет перевода: {e})"
            events.put(("cap_preview", text, tr, last_source[0] if tr else ""))
        threading.Thread(target=run, daemon=True).start()

    def cap_draw(self):
        c = self.cap_canvas
        w, h = int(c.cget("width")), int(c.cget("height"))
        c.delete("all")
        self.rounded(c, 1, 1, w - 1, h - 1, 16, fill=CARD_COLOR, outline=ACCENT_COLOR, width=2)
        lang = "RU" if self.cap.russian else "EN"
        tail = lambda s, px, room: s if self.measure(s, px) <= room else "…" + next(
            s[i:] for i in range(len(s)) if self.measure("…" + s[i:], px) <= room)
        # строка 1: что пишете
        self.rounded(c, 16, 16, 56, 44, 8, fill=ACCENT_COLOR, outline="")
        c.create_text(36, 30, text=lang, fill="white", font=(FONT_BOLD, -14))
        typed = self.cap.text
        c.create_text(70, 30, text=(tail(typed, 20, w - 110) if typed else "") + "|" if typed else
                      "Пишите по-русски…", fill=TEXT_COLOR if typed else "#6B7385", font=(FONT, -20), anchor="w")
        # строка 2: перевод и кто перевёл (справа: "нейросеть" / "Google" / "перевожу…")
        self.rounded(c, 16, 56, 56, 84, 8, fill="#26304A", outline="")
        c.create_text(36, 70, text="EN", fill="#AEB6C6", font=(FONT_BOLD, -14))
        if typed and (self.cap_pending or not self.cap_tr):
            badge, badge_color = "перевожу…", "#FFB547"
        elif typed:
            badge, badge_color = self.cap_src, "#3DDC97" if self.cap_src == "нейросеть" else "#8E97A8"
        else:
            badge, badge_color = "", ""
        bw = self.measure_font(badge, (FONT, -12)) + 18 if badge else 0
        if badge:
            self.rounded(c, w - 16 - bw, 59, w - 16, 81, 11, fill="", outline=badge_color, width=1)
            c.create_text(w - 16 - bw / 2, 70, text=badge, fill=badge_color, font=(FONT, -12))
        preview = self.cap_tr if typed else ""
        c.create_text(70, 70, text=tail(preview, 18, w - 110 - bw) if preview else "",
                      fill="#9FC3FF", font=(FONT, -18), anchor="w")
        # строка 3: подсказка клавишами
        other = "без сленга" if self.cap_casual else "со сленгом"
        keys = [("Enter", "вписать" + (" со сленгом" if self.cap_casual else "")), ("Shift+Enter", other),
                ("Ctrl+C", "скопировать"), ("Tab", "RU/EN"), ("Esc", "закрыть")]
        self.draw_keys(c, (w - self.keys_width(keys)) / 2, 112, keys)

    # ---- окно настроек ----
    def open_settings(self):
        if self.settings_win:
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        stop_hotkeys()            # иначе F-клавиши перехватываются и до окна не доходят
        BG, FIELD, MUTED = "#121722", "#232B3B", "#8E97A8"
        win = tk.Toplevel(self.root)
        win.title("Переводчик — настройки")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.iconbitmap(resource("icon.ico"))
        except tk.TclError:
            pass
        self.settings_win = win
        new = dict(settings)
        waiting = {}

        def heading(text, top=22):
            tk.Label(win, text=text, bg=BG, fg=TEXT_COLOR, font=(FONT_BOLD, -16), anchor="w").pack(
                fill="x", padx=24, pady=(top, 8))

        def note(text):
            tk.Label(win, text=text, bg=BG, fg=MUTED, font=(FONT, -12), anchor="w", justify="left",
                     wraplength=430).pack(fill="x", padx=24, pady=(4, 0))

        # клавиши: нажать на кнопку, потом нужную клавишу
        heading("Клавиши")
        buttons = {}

        def key_row(name, label):
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", padx=24, pady=3)
            tk.Label(row, text=label, bg=BG, fg=TEXT_COLOR, font=(FONT, -14), anchor="w").pack(side="left")
            b = tk.Label(row, text=key_name(new[name]), bg=FIELD, fg=TEXT_COLOR, font=(FONT_BOLD, -13), width=16,
                         pady=5, cursor="hand2", highlightthickness=1, highlightbackground="#3A4458")
            b.pack(side="right")

            def ask(e):
                for other, ob in buttons.items():
                    ob.configure(text=key_name(new[other]), fg=TEXT_COLOR, highlightbackground="#3A4458")
                waiting.clear()
                waiting[name] = b
                b.configure(text="нажмите клавишу…", fg=ACCENT_COLOR, highlightbackground=ACCENT_COLOR)
            b.bind("<Button-1>", ask)
            buttons[name] = b

        key_row("key_screen", "Перевод всего экрана")
        key_row("key_region", "Перевод области")
        key_row("key_type", "Написать по-английски")
        note("С Shift и Ctrl клавиши меняются вместе с основной: Shift+F7 — чат, Ctrl+F7 — новая область "
             "чата, Shift+F8 — отчёт, Ctrl+F8 — выход, Shift+F9 — наоборот по сленгу.")

        def on_key(e):
            if not waiting:
                if e.keycode == 0x1B:
                    close(False)
                    return "break"
                return None                  # обычный ввод (поле ключа)
            name, b = next(iter(waiting.items()))
            waiting.clear()
            vk = e.keycode
            b.configure(highlightbackground="#3A4458", fg=TEXT_COLOR)
            if vk not in (0x1B, 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x0D, 0x09, 0x08, 0x20):
                for other, ob in buttons.items():
                    if other != name and new[other] == vk:      # клавиша уже занята - меняем местами
                        new[other] = new[name]
                        ob.configure(text=key_name(new[other]))
                new[name] = vk
            b.configure(text=key_name(new[name]))
            return "break"
        win.bind("<KeyPress>", on_key)

        # перевод поверх игры
        heading("Перевод поверх игры")

        def slider(label, key, lo, hi):
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", padx=24)
            tk.Label(row, text=label, bg=BG, fg=TEXT_COLOR, font=(FONT, -14)).pack(side="left")
            value = tk.Label(row, bg=BG, fg=MUTED, font=(FONT, -13))
            value.pack(side="right")

            def changed(v):
                new[key] = int(float(v)) / 100
                value.configure(text=f"{int(float(v))}%")
            sc = tk.Scale(win, from_=lo, to=hi, orient="horizontal", showvalue=False, command=changed,
                          bg=ACCENT_COLOR, troughcolor=FIELD, activebackground="#7AA8FF", highlightthickness=0, bd=0,
                          sliderrelief="flat", sliderlength=22, width=12, length=430)
            sc.set(round(new[key] * 100))
            sc.pack(padx=24, pady=(4, 10), anchor="w")
            changed(sc.get())

        slider("Непрозрачность", "opacity", 40, 100)
        slider("Размер текста", "text_scale", 80, 140)

        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=24)
        tk.Label(row, text="Прятать перевод чата", bg=BG, fg=TEXT_COLOR, font=(FONT, -14)).pack(side="left")
        seg = tk.Frame(row, bg=BG)
        seg.pack(side="right")
        options = {}

        def pick(v):
            new["chat_hide"] = v
            for val, lbl in options.items():
                lbl.configure(bg=ACCENT_COLOR if val == v else FIELD, fg="white" if val == v else TEXT_COLOR)
        for val, text in ((5, "5 с"), (10, "10 с"), (30, "30 с"), (0, "никогда")):
            lbl = tk.Label(seg, text=text, font=(FONT, -13), padx=10, pady=4, cursor="hand2")
            lbl.bind("<Button-1>", lambda e, v=val: pick(v))
            lbl.pack(side="left", padx=(4, 0))
            options[val] = lbl
        pick(new["chat_hide"])
        note("Через столько секунд после последнего сообщения. Новое сообщение — перевод появится снова.")

        # переключатели
        heading("Перевод", top=12)
        flags = {}

        def check(label, key, value):
            flags[key] = tk.BooleanVar(value=value)
            tk.Checkbutton(win, text=label, variable=flags[key], bg=BG, fg=TEXT_COLOR, activebackground=BG,
                           activeforeground=TEXT_COLOR, selectcolor=FIELD, font=(FONT, -14), anchor="w",
                           highlightthickness=0, bd=0, padx=0).pack(fill="x", padx=22, pady=2)

        check("Переводить нейросетью — по смыслу (F9, F7 и чат)", "ai", new["ai"])
        check("F9 пишет со сленгом (Shift+F9 — наоборот)", "slang", new["slang"])
        check("Запускать вместе с Windows", "autostart", autostart_value() is not None)

        heading("Ключ нейросети Groq", top=14)
        key_var = tk.StringVar(value=ai_key())
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=24)
        entry = tk.Entry(row, textvariable=key_var, show="•", bg=FIELD, fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
                         relief="flat", font=(FONT, -13), highlightthickness=1, highlightbackground="#3A4458",
                         highlightcolor=ACCENT_COLOR)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        show = tk.Label(row, text="показать", bg=BG, fg=MUTED, font=(FONT, -12), cursor="hand2", padx=8)
        show.pack(side="left")
        show.bind("<Button-1>", lambda e: (entry.configure(show="" if entry.cget("show") else "•"),
                                          show.configure(text="скрыть" if not entry.cget("show") else "показать")))
        link = tk.Label(win, text="Получить свой бесплатный ключ — console.groq.com/keys", bg=BG, fg="#7AA8FF",
                        font=(FONT, -12, "underline"), cursor="hand2", anchor="w")
        link.pack(fill="x", padx=24, pady=(6, 0))
        link.bind("<Button-1>", lambda e: os.startfile(KEY_URL))
        note("У каждого свой ключ: бесплатный лимит у ключа общий на всех, кто им пользуется. "
             "Без ключа переводит Google.")
        region = settings["chat_region"]
        note(f"Область чата: {region[2] - region[0]}×{region[3] - region[1]} — выбрать заново: "
             f"Ctrl+{key_name(new['key_region'])}" if region else
             f"Область чата не выбрана — Shift+{key_name(new['key_region'])}, чтобы выбрать")

        def close(save):
            if save:
                new["ai"], new["slang"] = flags["ai"].get(), flags["slang"].get()
                if key_var.get().strip() != ai_key():
                    try:
                        save_key(key_var.get())
                    except OSError:
                        pass
                if new["ai"] and not settings["ai"]:
                    _ai_off_until[0] = 0
                settings.update({k: new[k] for k in settings if k != "chat_region"})
                save_settings()
                try:
                    set_autostart(flags["autostart"].get())
                except OSError:
                    pass
            self.settings_win = None
            win.destroy()
            start_hotkeys()
            if save:
                if self.tray:
                    self.tray.title = tray_title()
                    self.tray.update_menu()
                if self.live:                     # новая прозрачность и размер - сразу
                    mon, region = self.mon, self.chat
                    self.stop()
                    self.start(mon, region)
                self.show_toast("Настройки сохранены", "info", ms=1500)

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=24, pady=(20, 22))

        def button(text, cmd, primary):
            b = tk.Label(bar, text=text, bg=ACCENT_COLOR if primary else FIELD, fg="white" if primary else TEXT_COLOR,
                         font=(FONT_BOLD if primary else FONT, -14), padx=20, pady=7, cursor="hand2")
            b.bind("<Button-1>", lambda e: cmd())
            b.pack(side="right", padx=(10, 0))

        button("Сохранить", lambda: close(True), True)
        button("Отмена", lambda: close(False), False)
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.update_idletasks()
        try:      # тёмный заголовок окна (Windows 10/11)
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass
        mon = cursor_monitor()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{mon['left'] + (mon['width'] - w) // 2}+{mon['top'] + (mon['height'] - h) // 2}")
        win.focus_force()

    # ---- подсказки с клавишами ----
    KEY_FILL, KEY_LINE, KEY_TEXT, HINT_TEXT = "#232B3B", "#3A4458", "#E4E8EF", "#8E97A8"

    def keys_width(self, items):
        kf, lf = (FONT_BOLD, -12), (FONT, -13)
        w = 0
        for key, label in items:
            w += self.measure_font(key, kf) + 14 + 7 + self.measure_font(label, lf) + 20
        return w - 20

    def draw_keys(self, c, x, y, items):
        """Рисует ряд подсказок: [клавиша] что делает. x - левый край, y - середина строки."""
        kf, lf = (FONT_BOLD, -12), (FONT, -13)
        for key, label in items:
            kw = self.measure_font(key, kf) + 14
            self.rounded(c, x, y - 11, x + kw, y + 11, 6, fill=self.KEY_FILL, outline=self.KEY_LINE, width=1)
            c.create_text(x + kw / 2, y, text=key, fill=self.KEY_TEXT, font=kf)
            x += kw + 7
            c.create_text(x, y, text=label, fill=self.HINT_TEXT, font=lf, anchor="w")
            x += self.measure_font(label, lf) + 20

    def measure_font(self, text, font):
        key = (font, )
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family=font[0], size=font[1])
        return self.fonts[key].measure(text)

    def show_help(self, ms=None):
        ks, kr, kt = (key_name(settings[k]) for k in ("key_screen", "key_region", "key_type"))
        self.show_toast("Переводчик работает", "info", ms=ms, keys=[
            (ks, "весь экран"), (kr, "область"), ("Shift+" + kr, "чат"), (kt, "написать по-английски"),
            ("Ctrl+" + ks, "выход")],
            sub="Настройки, автозапуск и выход — в значке у часов")

    # ---- всплывающие плашки ----
    def show_toast(self, text, kind="info", ms=None, sub=None, mon=None, keys=None):
        self.hide_toast()
        mon = mon or cursor_monitor()
        tf = tkfont.Font(family=FONT_BOLD, size=-16)
        sf = tkfont.Font(family=FONT, size=-13)
        w = max(tf.measure(text), sf.measure(sub) if sub else 0, self.keys_width(keys) if keys else 0) + 76
        h = (108 if sub else 84) if keys else (64 if sub else 46)
        x = mon["left"] + (mon["width"] - w) // 2
        win, c = self.layer(w, h, x, mon["top"] + 28, alpha=0.96)
        exclude_from_capture(win)
        self.rounded(c, 1, 1, w - 1, h - 1, 16, fill=CARD_COLOR, outline=BORDER_COLOR, width=1)
        ix, iy = 26, (23 if keys else h / 2)
        if kind == "spin":
            c.create_oval(ix - 9, iy - 9, ix + 9, iy + 9, outline="#2A3344", width=3)
            arc = c.create_arc(ix - 9, iy - 9, ix + 9, iy + 9, start=90, extent=100,
                               style="arc", outline=ACCENT_COLOR, width=3)
            self.spin(c, arc, 90)
        else:
            color = {"info": "#3DDC97", "warn": "#FFB547", "error": "#FF5C5C"}[kind]
            c.create_oval(ix - 6, iy - 6, ix + 6, iy + 6, fill=color, outline="")
        tx = 48
        if keys:
            c.create_text(tx, 23, text=text, font=(FONT_BOLD, -16), fill=TEXT_COLOR, anchor="w")
            self.draw_keys(c, tx, 55, keys)
            if sub:
                c.create_text(tx, 87, text=sub, font=(FONT, -13), fill="#AEB6C6", anchor="w")
        elif sub:
            c.create_text(tx, 23, text=text, font=(FONT_BOLD, -16), fill=TEXT_COLOR, anchor="w")
            c.create_text(tx, 44, text=sub, font=(FONT, -13), fill="#AEB6C6", anchor="w")
        else:
            c.create_text(tx, h / 2, text=text, font=(FONT_BOLD, -16), fill=TEXT_COLOR, anchor="w")
        self.toast = win
        if ms:
            win.after(ms, lambda: self.toast is win and self.hide_toast())

    def spin(self, c, arc, angle):
        try:
            c.itemconfigure(arc, start=angle)
        except tk.TclError:
            return
        c.after(30, self.spin, c, arc, (angle - 12) % 360)

    def hide_toast(self):
        if self.toast:
            self.toast.destroy()
            self.toast = None

    def run(self):
        if self.duplicate:
            self.show_toast("Переводчик уже запущен", "info", ms=2500,
                            sub="Он работает в фоне — значок у часов")
            self.root.after(2500, self.root.destroy)
            self.root.mainloop()
            os._exit(0)
        self.tray = tray_thread()
        if "--autostart" in sys.argv:
            self.show_toast("Переводчик запущен", "info", ms=2500, sub="Значок у часов — меню и настройки")
        else:
            self.show_help(ms=5000)
        self.root.mainloop()
        if self.tray:
            self.tray.stop()                       # иначе значок в трее не даст программе закрыться
        os._exit(0)


if __name__ == "__main__":
    try:
        App().run()
    except Exception:
        # без консоли ошибку иначе не увидеть - пишем в файл рядом
        with open(os.path.join(APP_DIR, "error.log"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
