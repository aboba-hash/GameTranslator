"""
Самопроверка переводчика: запускается через "Проверка.bat" (или python проверка.py).
Проверяет всё по очереди и пишет, что работает, а что нет. Кэш переводов не трогает.
  python проверка.py          - всё, включая F9 в Блокноте (на ~10 секунд откроется Блокнот)
  python проверка.py --быстро - без Блокнота
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
results = []


def check(name, fn):
    t = time.time()
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, f"{type(e).__name__}: {e}"
        if os.environ.get("DEBUG"):
            traceback.print_exc()
    ok = ok if ok is None else bool(ok)       # None - предупреждение: не ошибка, но стоит знать
    mark = {True: "✅", False: "❌", None: "⚠️"}[ok]
    print(f"{mark} {name}  ({time.time() - t:.1f} с)")
    for line in str(info).splitlines():
        print(f"     {line}")
    results.append(ok)
    return ok


# ---------- 1. библиотеки ----------
def libs():
    import mss, PIL  # noqa: F401
    from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    return True, f"Python {sys.version.split()[0]}, Pillow {PIL.__version__}"


if not check("Библиотеки установлены", libs):
    print("\nСначала запустите 'Установить библиотеки.bat'.")
    input("Enter - закрыть")
    sys.exit(1)

import translator as tr  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

tr.cache.clear()
tr.save_cache = lambda: None          # проверка не засоряет кэш переводов


def drain():
    out = []
    while not tr.events.empty():
        out.append(tr.events.get())
    return out


# ---------- 2. распознавание ----------
def make_image(red_underline=False):
    img = Image.new("RGB", (900, 220), (250, 250, 250))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("segoeui.ttf", 22)
    lines = ["Welcome to the game, traveler!", "Press E to open your inventory. Press M to view the map.",
             "Warning: wolves are hunting at night. Stay close to the campfire."]
    for i, text in enumerate(lines):
        y = 30 + i * 60
        d.text((30, y), text, font=font, fill=(20, 20, 20))
        if red_underline:      # как проверка орфографии в Блокноте
            w = d.textlength(text, font=font)
            for x in range(30, int(30 + w), 4):
                d.line([(x, y + 31), (x + 2, y + 29), (x + 4, y + 31)], fill=(230, 30, 30), width=1)
    return img, lines


def ocr_ok(red):
    img, lines = make_image(red)
    _, blocks = tr.analyze(img)
    got = " ".join(b["text"] for b in blocks).lower()
    words = [w.strip(".,!:").lower() for l in lines for w in l.split()]
    found = sum(w in got for w in words)
    return found >= 0.9 * len(words), f"узнано слов: {found} из {len(words)}\n«{got[:110]}…»"


def ocr_lang():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    en = OcrEngine.try_create_from_language(Language("en"))
    ru = OcrEngine.try_create_from_language(Language("ru"))
    return en is not None, f"английский: {'есть' if en else 'НЕТ - добавьте язык в Параметрах Windows'}, " \
                           f"русский: {'есть' if ru else 'нет (не обязателен)'}"


check("Распознавание текста Windows (языки)", ocr_lang)
check("Распознавание: обычный текст", lambda: ocr_ok(False))
check("Распознавание: текст с красным подчёркиванием", lambda: ocr_ok(True))


# ---------- 3. переводчики ----------
def google():
    try:
        out = tr._google("Welcome to the game", "en", "ru")
    except tr.Blocked as e:
        return None, f"{e} - это временно (обычно до часа), пока программа сама переводит через Яндекс"
    return any(c in out.lower() for c in "абвгдеж"), f"«Welcome to the game» -> «{out}»"


def yandex():
    out = tr._yandex(["Welcome to the game"], "en", "ru")[0]
    return any(c in out.lower() for c in "абвгдеж"), f"запасной: «{out}»"


def ai():
    key = tr.ai_key()
    if not key:
        return None, ("ключ не введён — нейросеть выключена, переводит Google.\n"
                      "Свой бесплатный ключ: значок у часов → Настройки")
    tr._ai_off_until[0] = 0
    out = tr.ai_to_en("мб сольемся? все равно не вывезем", True)
    ev = drain()
    if ev:
        return False, ev[0][1]
    return True, f"модель {tr.AI_MODEL}\n«мб сольемся? все равно не вывезем» -> «{out}»"


def ai_fallback():
    """Эмулируем ответ 429 (лимит кончился): должна отключиться и перевод пойти через Google."""
    class Resp:
        status = 429
        def read(self): return b"{}"
        def getheader(self, h): return "30"

    class Conn:
        sock = None
        timeout = 0
        def request(self, *a, **k): pass
        def getresponse(self): return Resp()
        def close(self): pass

    tr._ai_off_until[0] = 0
    real_key = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = "gsk_test"          # ключ для проверки: ответ "лимит" подставляем сами
    old, tr._ai_conn[0] = tr._ai_conn[0], Conn()
    try:
        out = tr.translate_to("го в мид", "en", True)
        ev = drain()
        off = tr._ai_off_until[0] - time.time()
    finally:
        tr._ai_conn[0] = None
        tr._ai_off_until[0] = 0
        if real_key is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = real_key
    ok = off > 0 and ev and "лимит" in ev[0][1] and out.strip()
    return ok, f"сообщение: «{ev[0][1] if ev else '-'}»\nперевод без нейросети: «{out}»"


def mat():
    cases = {"иди нахуй": "fuck yourself", "пиздато": "awesome", "хуево играешь": "shit", "тебе пизда": "fucked"}
    bad = []
    for ru, need in cases.items():
        en = tr.online_translate([tr.normalize_ru(ru)], "ru", "en", spell=True)[0]
        if need not in en.lower():
            bad.append(f"«{ru}» -> «{en}»")
    return not bad, "\n".join(bad) or "словарь мата (запасной путь) - всё по смыслу"


def f7_ai():
    if not tr.ai_key():
        return None, "пропущено: ключ нейросети не введён"
    img, _ = make_image(True)
    tr._ai_off_until[0] = 0
    blocks = tr.process(img, ai=True)
    ev = drain()
    text = "\n".join(b["tr"] for b in blocks)
    ok = not ev and "инвентар" in text.lower() and "карт" in text.lower()
    return ok, (ev[0][1] + "\n" if ev else "") + text


check("Google Переводчик", google)
check("Яндекс Переводчик (запасной)", yandex)
check("Нейросеть Groq (F9 по смыслу)", ai)
check("F7 через нейросеть", f7_ai)
check("Лимит нейросети кончился -> обычный переводчик", ai_fallback)
check("Мат без нейросети (Google/Яндекс)", mat)


# ---------- 4. F9 в Блокноте ----------
def f9_notepad(runs=5):
    u = ctypes.windll.user32
    u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    u.SendMessageW.restype = ctypes.c_ssize_t
    real_translate = tr.translate_to
    tr.translate_to = lambda text, lang, casual=False: "EN<" + text + ">"   # проверяем только замену текста
    path = os.path.join(tempfile.gettempdir(), "проверка_F9.txt")
    open(path, "w", encoding="utf-8").write("x")
    subprocess.Popen(["notepad.exe", path])
    top = edit = None
    cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    for _ in range(50):
        time.sleep(0.2)
        found = []

        def enum_top(h, _):
            b = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, b, 256)
            if "проверка_F9" in b.value and u.IsWindowVisible(h):
                found.append(h)
            return True
        u.EnumWindows(cb(enum_top), 0)
        if found:
            top = found[0]
            edit = tr.edit_control(top) or None
            if edit is None:
                kids = []

                def enum_child(h, _):
                    b = ctypes.create_unicode_buffer(64)
                    u.GetClassNameW(h, b, 64)
                    if b.value.lower().startswith(("richedit", "edit")):
                        kids.append(h)
                    return True
                u.EnumChildWindows(top, cb(enum_child), 0)
                edit = kids[0] if kids else None
            if edit:
                break
    if not edit:
        tr.translate_to = real_translate
        return False, "не удалось открыть Блокнот"
    ok = 0
    try:
        for i in range(runs):
            src = f"привет как дела {i}"
            u.SendMessageW(edit, 0x0C, None, ctypes.cast(ctypes.c_wchar_p(src), ctypes.c_void_p))
            u.SendMessageW(edit, 0xB1, 0, -1)
            u.keybd_event(0x12, 0, 0, 0)
            u.SetForegroundWindow(top)
            u.keybd_event(0x12, 0, 2, 0)          # Alt: заодно проверяем случай "фокус ушёл в меню"
            time.sleep(0.4)
            tr.set_clipboard_text("старый буфер")
            drain()
            tr.type_translate(True)
            time.sleep(0.3)
            n = u.SendMessageW(edit, 0x0E, None, None)
            b = ctypes.create_unicode_buffer(n + 1)
            u.SendMessageW(edit, 0x0D, n + 1, ctypes.cast(b, ctypes.c_void_p))
            ok += b.value == f"EN<{src}>" and tr.get_clipboard_text() == "старый буфер"
    finally:
        tr.translate_to = real_translate
        u.SendMessageW(edit, 0x0C, None, ctypes.cast(ctypes.c_wchar_p(""), ctypes.c_void_p))
        u.PostMessageW(top, 0x10, 0, 0)
    return ok == runs, f"заменено {ok} из {runs} (с нажатым Alt), буфер обмена возвращается"


if "--быстро" not in sys.argv:
    print("\nСейчас на ~10 секунд откроется Блокнот - не трогайте клавиатуру и мышь.")
    time.sleep(2)
    check("F9 в Блокноте: выделить -> заменить английским", f9_notepad)

bad, warn = results.count(False), results.count(None)
print("\n" + (f"Не работает: {bad} из {len(results)}. Подробности выше." if bad else
              "Всё работает" + (f", предупреждений: {warn} (см. ⚠️ выше)." if warn else ".")))
try:
    input("Enter - закрыть")
except EOFError:
    pass
