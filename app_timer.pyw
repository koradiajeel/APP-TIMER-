"""App Timer - tracks laptop screen time and per-app usage on Windows.

Normal apps count the whole time they are open (front or background).
Watched apps only count while they are in front, and you get a reminder
when one of them sits in the background too long.

No extra packages needed: only the Python standard library.
"""

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
import winreg
import winsound

APP_NAME = "App Timer"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AppTimer")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
LOG_FILE = os.path.join(DATA_DIR, "error.log")
TICK = 1.0            # seconds between checks
MAX_GAP = 15.0        # a longer gap means sleep/hibernate -> not counted
SAVE_EVERY = 30.0     # seconds between autosaves
DEFAULT_REMIND = 60   # seconds in background before a watched app reminds you
SNOOZE = 10 * 60
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AppTimer"


def log_error():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n{traceback.format_exc()}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
version = ctypes.WinDLL("version")

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wt.LONG
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
version.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wt.UINT)]

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
SW_RESTORE = 9
SPI_GETWORKAREA = 0x0030

# Shell / system windows that are not "apps"
IGNORED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
IGNORED_EXES = {
    "applicationframehost.exe", "textinputhost.exe", "shellexperiencehost.exe",
    "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe", "lockapp.exe",
    "shellhost.exe",
}


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_cloaked(hwnd):
    val = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)) == 0:
        return val.value != 0
    return False


def uwp_real_pid(hwnd, frame_pid):
    """Store apps live inside ApplicationFrameHost; find the real app process."""
    found = []

    def cb(child, _):
        p = window_pid(child)
        if p != frame_pid:
            found.append(p)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return found[0] if found else frame_pid


def is_session_locked():
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def file_description(path):
    """Friendly app name from the exe's version info, e.g. 'Google Chrome'."""
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wt.UINT()
        langs = []
        if version.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)) \
                and length.value >= 4:
            pair = ctypes.cast(ptr, ctypes.POINTER(wt.WORD * 2)).contents
            langs.append((pair[0], pair[1]))
        langs += [(0x0409, 0x04B0), (0x0409, 0x04E4)]
        for lang, cp in langs:
            key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
            if version.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(length)) and length.value > 1:
                text = ctypes.wstring_at(ptr.value, length.value).strip("\x00 ").strip()
                if text:
                    return text
    except Exception:
        pass
    return None


def pretty_exe(exe):
    name = exe[:-4] if exe.endswith(".exe") else exe
    return name[:1].upper() + name[1:]


def bring_to_front(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    return bool(user32.SetForegroundWindow(hwnd))


def work_area():
    rect = wt.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


shell32 = ctypes.WinDLL("shell32")
gdi32 = ctypes.WinDLL("gdi32")
shell32.SHDefExtractIconW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.UINT, ctypes.POINTER(wt.HICON),
                                      ctypes.POINTER(wt.HICON), wt.UINT]
shell32.SHDefExtractIconW.restype = ctypes.c_long
user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON, ctypes.c_int, ctypes.c_int,
                              wt.UINT, wt.HBRUSH, wt.UINT]
user32.DestroyIcon.argtypes = [wt.HICON]
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def icon_ppm(path, size, bg_hex):
    """The exe's own icon, drawn onto bg_hex, as PPM image bytes."""
    hicon = wt.HICON()
    if shell32.SHDefExtractIconW(path, 0, 0, ctypes.byref(hicon), None, size) != 0 or not hicon:
        return None
    n = size * size
    hdc = gdi32.CreateCompatibleDC(None)
    try:
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbmp:
            return None
        old = gdi32.SelectObject(hdc, hbmp)
        r, g, b = int(bg_hex[1:3], 16), int(bg_hex[3:5], 16), int(bg_hex[5:7], 16)
        ctypes.memmove(bits, bytes((b, g, r, 255)) * n, n * 4)
        user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 3)
        raw = ctypes.string_at(bits, n * 4)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
    finally:
        gdi32.DeleteDC(hdc)
        user32.DestroyIcon(hicon)
    rgb = bytearray(n * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    return b"P6 %d %d 255\n" % (size, size) + bytes(rgb)


def dark_title_bar(win):
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        if dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4) != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)
    except Exception:
        pass


class WindowScanner:
    """Finds which apps have open windows and which one is in front."""

    def __init__(self):
        self.own_pid = os.getpid()
        self.path_cache = {}
        self.cache_time = time.time()

    def _path(self, pid):
        if pid not in self.path_cache:
            self.path_cache[pid] = process_path(pid)
        return self.path_cache[pid]

    def _resolve(self, hwnd):
        pid = window_pid(hwnd)
        path = self._path(pid)
        if path and os.path.basename(path).lower() == "applicationframehost.exe":
            pid = uwp_real_pid(hwnd, pid)
            path = self._path(pid)
        return pid, path

    def scan(self):
        """Returns (apps, fg_exe, raw_fg_exe). apps = {exe: (path, hwnd)}."""
        if time.time() - self.cache_time > 60:
            self.path_cache.clear()
            self.cache_time = time.time()

        apps = {}

        def cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                    return True
                if user32.GetWindow(hwnd, GW_OWNER):
                    return True
                if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                    return True
                if is_cloaked(hwnd) or class_name(hwnd) in IGNORED_CLASSES:
                    return True
                pid, path = self._resolve(hwnd)
                if not path or pid == self.own_pid:
                    return True
                exe = os.path.basename(path).lower()
                if exe not in IGNORED_EXES:
                    apps.setdefault(exe, (path, hwnd))
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)

        fg = raw_fg = None
        hwnd = user32.GetForegroundWindow()
        if hwnd and class_name(hwnd) not in IGNORED_CLASSES:
            pid, path = self._resolve(hwnd)
            if path:
                raw_fg = os.path.basename(path).lower()
                if pid != self.own_pid and raw_fg not in IGNORED_EXES:
                    fg = raw_fg
                    apps[fg] = (path, hwnd)
        return apps, fg, raw_fg


# --------------------------------------------------------------------------
# Data storage
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {
            "settings": {"reminders": True, "sound": True, "close_hint_shown": False},
            "watched": {},   # exe -> {"remind_after": seconds}
            "names": {},     # exe -> friendly name
            "paths": {},     # exe -> full path (for icons)
            "days": {},      # "YYYY-MM-DD" -> {"screen": s, "apps": {exe: {...}}}
        }
        self.load()

    def load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                    self.data[key].update(value)
                else:
                    self.data[key] = value
        except Exception:
            log_error()
            try:
                os.replace(DATA_FILE, DATA_FILE + f".broken-{int(time.time())}")
            except Exception:
                pass

    def save(self):
        try:
            with self.lock:
                text = json.dumps(self.data, indent=1)
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, DATA_FILE)
        except Exception:
            log_error()

    def day(self, key):
        return self.data["days"].setdefault(key, {"screen": 0.0, "apps": {}})

    def name(self, exe):
        return self.data["names"].get(exe) or pretty_exe(exe)


# --------------------------------------------------------------------------
# Background tracker
# --------------------------------------------------------------------------
class Tracker(threading.Thread):
    def __init__(self, store, notify_queue):
        super().__init__(daemon=True)
        self.store = store
        self.notify_queue = notify_queue
        self.scanner = WindowScanner()
        self.stop_event = threading.Event()
        self.away = {}           # watched exe -> {"since": t, "last": t|None}
        self.snooze_until = {}   # exe -> t
        self.hwnds = {}          # exe -> last seen window
        self.open_now = set()
        self.fg_now = None
        self.locked = False

    def run(self):
        last = time.time()
        last_save = last
        while not self.stop_event.wait(TICK):
            now = time.time()
            gap = now - last
            last = now
            try:
                self.tick(now, gap)
            except Exception:
                log_error()
            if now - last_save >= SAVE_EVERY:
                self.store.save()
                last_save = now

    def tick(self, now, gap):
        apps, fg, raw_fg = self.scanner.scan()
        locked = raw_fg == "lockapp.exe" or is_session_locked()
        counting = not locked and 0 < gap <= MAX_GAP
        today = dt.date.today().isoformat()

        with self.store.lock:
            data = self.store.data
            watched = dict(data["watched"])
            reminders_on = data["settings"].get("reminders", True)
            for exe, (path, hwnd) in apps.items():
                self.hwnds[exe] = hwnd
                if exe not in data["names"]:
                    data["names"][exe] = file_description(path) or pretty_exe(exe)
                data["paths"][exe] = path
            if counting:
                day = self.store.day(today)
                day["screen"] += gap
                hours = day.setdefault("hours", [0.0] * 24)
                hours[dt.datetime.now().hour] += gap
                for exe in apps:
                    rec = day["apps"].setdefault(exe, {"counted": 0.0, "active": 0.0, "open": 0.0})
                    rec["open"] += gap
                    if exe == fg:
                        rec["active"] += gap
                    # watched apps only count while in front; others count while open
                    if exe not in watched or exe == fg:
                        rec["counted"] += gap

        self.open_now = set(apps)
        self.fg_now = fg
        self.locked = locked

        # ---- reminders for watched apps sitting in the background ----
        for exe in list(self.away):
            if exe not in apps or exe not in watched or exe == fg:
                del self.away[exe]
        if locked or not counting:
            self.away.clear()
            return
        for exe, cfg in watched.items():
            if exe not in apps or exe == fg:
                continue
            state = self.away.setdefault(exe, {"since": now, "last": None})
            if not reminders_on or now < self.snooze_until.get(exe, 0):
                continue
            delay = max(5, float(cfg.get("remind_after", DEFAULT_REMIND)))
            ref = state["last"] or state["since"]
            if now - ref >= delay:
                state["last"] = now
                self.notify_queue.put((exe, now - state["since"]))


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def fmt(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def startup_command():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass



# ==========================================================================
# Look & feel
# ==========================================================================
BG = "#0f1117"
SIDEBAR = "#141720"
CARD = "#1a1e29"
CARD_HI = "#252a38"
HOVER = "#1f2433"
BORDER = "#262c3b"
TEXT = "#e9ebf1"
MUTED = "#8b93a7"
DIM = "#5f6779"
ACCENT = "#7c8cff"
ACCENT_HI = "#98a5ff"
ACCENT_DIM = "#3b4480"
WATCH = "#f5a524"
GOOD = "#34d399"
BAD = "#f87171"
TRACK = "#262b39"
PALETTE = ["#7c8cff", "#34d399", "#f472b6", "#fbbf24", "#60a5fa", "#a78bfa", "#f87171", "#2dd4bf"]

SCALE = 1.0
UI_FONT = "Segoe UI"
ICON_FONT = "Segoe MDL2 Assets"
REMIND_STEPS = [0.5, 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120]


def px(n):
    return int(round(n * SCALE))


def F(size, weight="normal"):
    return (UI_FONT, size, weight)


def setup_fonts(root):
    global SCALE, ICON_FONT
    SCALE = root.winfo_fpixels("1i") / 96
    if "Segoe Fluent Icons" in set(tkfont.families(root)):
        ICON_FONT = "Segoe Fluent Icons"


def color_for(exe):
    return PALETTE[sum(map(ord, exe)) % len(PALETTE)]


def short(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def round_rect(c, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r < 1:
        return c.create_rectangle(x0, y0, x1, y1, outline="", **kw)
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, outline="", **kw)


def greeting():
    h = dt.datetime.now().hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 22:
        return "Good evening"
    return "Working late"


# ==========================================================================
# Widgets
# ==========================================================================
class FlatButton(tk.Label):
    KINDS = {
        "primary": (ACCENT, ACCENT_HI, "#0d1020"),
        "ghost": (CARD_HI, "#30364a", TEXT),
        "subtle": (CARD, HOVER, MUTED),
        "side": (SIDEBAR, HOVER, MUTED),
        "danger": (CARD_HI, "#3b2530", BAD),
        "watch": ("#3a2d14", "#4a3a1a", WATCH),
    }

    def __init__(self, parent, text, command=None, kind="ghost", font=None, padx=14, pady=6):
        super().__init__(parent, text=text, font=font or F(10, "bold"), padx=px(padx), pady=px(pady),
                         cursor="hand2", bd=0)
        self.command = command
        self.set_kind(kind)
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<ButtonRelease-1>", self._click)

    def set_kind(self, kind, text=None):
        self.kind = kind
        self._bg, self._hover, fg = self.KINDS[kind]
        self.configure(bg=self._bg, fg=fg)
        if text is not None:
            self.configure(text=text)

    def _click(self, e):
        if self.command and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()


class Toggle(tk.Canvas):
    def __init__(self, parent, value, command, bg=CARD):
        super().__init__(parent, width=px(44), height=px(24), bg=bg, highlightthickness=0, cursor="hand2")
        self.value, self.command = value, command
        self.bind("<Button-1>", self.flip)
        self.draw()

    def set(self, value):
        self.value = value
        self.draw()

    def flip(self, _=None):
        self.set(not self.value)
        self.command(self.value)

    def draw(self):
        self.delete("all")
        w, h = px(44), px(24)
        round_rect(self, 0, 0, w, h, h / 2, fill=ACCENT if self.value else "#3a4052")
        k = h - px(6)
        x = w - k - px(3) if self.value else px(3)
        self.create_oval(x, px(3), x + k, px(3) + k, fill="white", outline="")


class Bar(tk.Canvas):
    def __init__(self, parent, bg=CARD, height=8):
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0)
        self.frac, self.color = 0.0, ACCENT
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, frac, color):
        frac = max(0.0, min(1.0, frac))
        if abs(frac - self.frac) > 0.002 or color != self.color:
            self.frac, self.color = frac, color
            self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        round_rect(self, 0, 0, w, h, h / 2, fill=TRACK)
        if self.frac > 0:
            round_rect(self, 0, 0, max(h, w * self.frac), h, h / 2, fill=self.color)


class AppIcon(tk.Canvas):
    def __init__(self, parent, app, exe, size=32, bg=CARD):
        self.s = px(size)
        super().__init__(parent, width=self.s, height=self.s, bg=bg, highlightthickness=0)
        self.app, self.exe, self.has_img = app, False, False
        self.set(exe)

    def set(self, exe):
        if exe == self.exe and (self.has_img or not exe):
            return
        self.exe = exe
        self.delete("all")
        s = self.s
        self.has_img = False
        if not exe:
            round_rect(self, 0, 0, s, s, s * 0.28, fill=CARD_HI)
            self.create_text(s / 2, s / 2, text="", fill=ACCENT,
                             font=(ICON_FONT, max(8, int(s / SCALE * 0.36))))
            return
        img = self.app.icon_for(exe, s)
        if img:
            self.has_img = True
            self.img = img
            self.create_image(s / 2, s / 2, image=img)
        else:
            round_rect(self, 0, 0, s, s, s * 0.28, fill=color_for(exe))
            self.create_text(s / 2, s / 2, text=self.app.store.name(exe)[:1].upper(),
                             fill="#0d1020", font=F(max(8, int(s / SCALE * 0.36)), "bold"))


class Stepper(tk.Frame):
    def __init__(self, parent, value, on_change, bg=CARD):
        super().__init__(parent, bg=bg)
        self.on_change = on_change
        FlatButton(self, "−", lambda: self.step(-1), font=F(10, "bold"), padx=9, pady=3).pack(side="left")
        self.lbl = tk.Label(self, bg=bg, fg=TEXT, font=F(10, "bold"), width=7)
        self.lbl.pack(side="left")
        FlatButton(self, "+", lambda: self.step(1), font=F(10, "bold"), padx=9, pady=3).pack(side="left")
        self.set(value)

    def set(self, minutes):
        self.value = minutes
        self.lbl.configure(text=f"{minutes:g} min")

    def step(self, d):
        idx = min(range(len(REMIND_STEPS)), key=lambda i: abs(REMIND_STEPS[i] - self.value))
        idx = max(0, min(len(REMIND_STEPS) - 1, idx + d))
        self.set(REMIND_STEPS[idx])
        self.on_change(self.value)


class ScrollFrame(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG, padx=px(34), pady=px(28))
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))

    def scroll(self, delta):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-delta / 120) * 3, "units")


class NavItem(tk.Frame):
    def __init__(self, parent, glyph, text, command):
        super().__init__(parent, bg=SIDEBAR, cursor="hand2")
        self.active = False
        self.ind = tk.Frame(self, bg=SIDEBAR, width=px(3))
        self.ind.pack(side="left", fill="y")
        self.ic = tk.Label(self, text=glyph, font=(ICON_FONT, 12), bg=SIDEBAR, fg=MUTED)
        self.ic.pack(side="left", padx=(px(18), px(12)), pady=px(10))
        self.tx = tk.Label(self, text=text, font=F(10), bg=SIDEBAR, fg=MUTED)
        self.tx.pack(side="left")
        for w in (self, self.ic, self.tx):
            w.bind("<Enter>", lambda e: self.paint(hover=True))
            w.bind("<Leave>", lambda e: self.paint())
            w.bind("<Button-1>", lambda e: command())

    def set_active(self, on):
        self.active = on
        self.paint()

    def paint(self, hover=False):
        bg = HOVER if (hover or self.active) else SIDEBAR
        fg = TEXT if (self.active or hover) else MUTED
        for w in (self, self.ic, self.tx):
            w.configure(bg=bg)
        self.ic.configure(fg=ACCENT if self.active else fg)
        self.tx.configure(fg=fg, font=F(10, "bold" if self.active else "normal"))
        self.ind.configure(bg=ACCENT if self.active else bg)


class RowList:
    """Keeps a list of row widgets in sync with data without flicker."""

    def __init__(self, parent, factory):
        self.parent, self.factory = parent, factory
        self.rows, self.order = {}, []

    def sync(self, items):
        keys = [k for k, _ in items]
        for k in list(self.rows):
            if k not in keys:
                self.rows.pop(k).destroy()
        for k, kw in items:
            row = self.rows.get(k)
            if row is None:
                row = self.rows[k] = self.factory(self.parent, k)
            row.update_row(**kw)
        if keys != self.order:
            for k in keys:
                self.rows[k].pack_forget()
            for k in keys:
                self.rows[k].pack(fill="x")
            self.order = keys


def app_columns(frame, full):
    frame.columnconfigure(0, minsize=px(46))
    frame.columnconfigure(1, minsize=px(230))
    frame.columnconfigure(2, weight=1)
    frame.columnconfigure(3, minsize=px(80))
    if full:
        frame.columnconfigure(4, minsize=px(100))
        frame.columnconfigure(5, minsize=px(110))
        frame.columnconfigure(6, minsize=px(118))


class AppRow(tk.Frame):
    def __init__(self, parent, app, exe, full=False):
        super().__init__(parent, bg=CARD)
        self.app, self.exe, self.full = app, exe, full
        app_columns(self, full)
        self.icon = AppIcon(self, app, exe)
        self.icon.grid(row=0, column=0, rowspan=2, sticky="w", pady=px(7))
        self.name = tk.Label(self, bg=CARD, fg=TEXT, font=F(10, "bold"), anchor="w")
        self.name.grid(row=0, column=1, sticky="sw")
        sub = tk.Frame(self, bg=CARD)
        sub.grid(row=1, column=1, sticky="nw")
        self.status = tk.Label(sub, bg=CARD, fg=MUTED, font=F(8))
        self.status.pack(side="left")
        self.badge = tk.Label(sub, bg=CARD, fg=WATCH, font=F(8, "bold"), text="WATCHED")
        self.bar = Bar(self)
        self.bar.grid(row=0, column=2, rowspan=2, sticky="ew", padx=px(16))
        self.time = tk.Label(self, bg=CARD, fg=TEXT, font=F(10, "bold"), anchor="e")
        self.time.grid(row=0, column=3, rowspan=2, sticky="e")
        if full:
            self.active = tk.Label(self, bg=CARD, fg=MUTED, font=F(9), anchor="e")
            self.active.grid(row=0, column=4, rowspan=2, sticky="e")
            self.open = tk.Label(self, bg=CARD, fg=MUTED, font=F(9), anchor="e")
            self.open.grid(row=0, column=5, rowspan=2, sticky="e")
            self.btn = FlatButton(self, "Watch", lambda: app.toggle_watch(exe), font=F(9, "bold"), padx=10, pady=4)
            self.btn.grid(row=0, column=6, rowspan=2, sticky="e")
        self.watched = None

    def update_row(self, name, status, status_color, watched, frac, time_text, active=None, open_=None):
        self.icon.set(self.exe)
        self.name.configure(text=short(name, 28))
        self.status.configure(text=status, fg=status_color)
        if watched != self.watched:
            self.watched = watched
            if watched:
                self.badge.pack(side="left", padx=(px(6), 0))
            else:
                self.badge.pack_forget()
            if self.full:
                self.btn.set_kind("watch" if watched else "ghost", "Watching" if watched else "Watch")
        self.bar.set(frac, WATCH if watched else ACCENT)
        self.time.configure(text=time_text)
        if self.full:
            self.active.configure(text=active)
            self.open.configure(text=open_)


class WatchRow(tk.Frame):
    def __init__(self, parent, app, exe):
        super().__init__(parent, bg=CARD)
        self.app, self.exe = app, exe
        AppIcon(self, app, exe, size=36).pack(side="left", pady=px(9))
        mid = tk.Frame(self, bg=CARD)
        mid.pack(side="left", fill="x", expand=True, padx=px(14))
        self.name = tk.Label(mid, bg=CARD, fg=TEXT, font=F(10, "bold"), anchor="w")
        self.name.pack(anchor="w")
        st = tk.Frame(mid, bg=CARD)
        st.pack(anchor="w")
        self.dot = tk.Canvas(st, width=px(8), height=px(8), bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, px(6)))
        self.status = tk.Label(st, bg=CARD, fg=MUTED, font=F(9))
        self.status.pack(side="left")
        FlatButton(self, "Remove", lambda: app.remove_watch(exe), kind="danger",
                   font=F(9, "bold"), padx=10, pady=4).pack(side="right")
        self.stepper = Stepper(self, 1, lambda m: app.set_remind(exe, m))
        self.stepper.pack(side="right", padx=px(16))
        tk.Label(self, bg=CARD, fg=MUTED, font=F(9), text="Remind after").pack(side="right")
        self.color = None

    def update_row(self, name, status, color, minutes):
        self.name.configure(text=short(name, 34))
        self.status.configure(text=status)
        if color != self.color:
            self.color = color
            self.dot.delete("all")
            self.dot.create_oval(0, 0, px(8), px(8), fill=color, outline="")
        if self.stepper.value != minutes:
            self.stepper.set(minutes)


def card(parent, **kw):
    return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=BORDER, padx=px(20), pady=px(16), **kw)


def caption(parent, text, bg=CARD):
    lbl = tk.Label(parent, text=text.upper(), bg=bg, fg=MUTED, font=F(8, "bold"), anchor="w")
    lbl.pack(anchor="w")
    return lbl


def card_header(parent, title, right=None):
    row = tk.Frame(parent, bg=CARD)
    row.pack(fill="x", pady=(0, px(8)))
    tk.Label(row, text=title, bg=CARD, fg=TEXT, font=F(12, "bold")).pack(side="left")
    if right:
        right(row).pack(side="right")
    return row


def page_title(parent, title, subtitle=None):
    head = tk.Frame(parent, bg=BG)
    head.pack(fill="x", pady=(0, px(18)))
    t = tk.Label(head, text=title, bg=BG, fg=TEXT, font=F(20, "bold"))
    t.pack(anchor="w")
    s = tk.Label(head, text=subtitle or "", bg=BG, fg=MUTED, font=F(10), justify="left", anchor="w",
                 wraplength=px(780))
    s.pack(anchor="w")
    return head, t, s


# ==========================================================================
# Reminder popup
# ==========================================================================
class Reminder(tk.Toplevel):
    def __init__(self, app, exe, away_seconds, slot):
        super().__init__(app.root)
        self.app, self.exe, self.slot = app, exe, slot
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.configure(bg=BORDER)
        outer = tk.Frame(self, bg=CARD)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(outer, bg=WATCH, width=px(4)).pack(side="left", fill="y")
        body = tk.Frame(outer, bg=CARD, padx=px(18), pady=px(16))
        body.pack(side="left", fill="both", expand=True)

        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x")
        AppIcon(top, app, exe, size=38).pack(side="left", anchor="n")
        txt = tk.Frame(top, bg=CARD)
        txt.pack(side="left", fill="x", padx=(px(12), 0))
        tk.Label(txt, text=f"Come back to {short(app.store.name(exe), 26)}", bg=CARD, fg=TEXT,
                 font=F(11, "bold"), anchor="w").pack(anchor="w")
        tk.Label(txt, text=f"In the background for {fmt(away_seconds)}, so its timer is paused.",
                 bg=CARD, fg=MUTED, font=F(9), anchor="w", wraplength=px(290), justify="left").pack(anchor="w")

        btns = tk.Frame(body, bg=CARD)
        btns.pack(fill="x", pady=(px(14), 0))
        FlatButton(btns, "Go back", self.go_back, kind="primary").pack(side="left")
        FlatButton(btns, "Snooze 10 min", self.snooze).pack(side="left", padx=px(8))
        FlatButton(btns, "Dismiss", self.close, kind="subtle").pack(side="right")

        self.update_idletasks()
        w, h = px(410), self.winfo_reqheight()
        area = work_area()
        right, bottom = (area[2], area[3]) if area else (self.winfo_screenwidth(), self.winfo_screenheight() - 48)
        x = right - w - px(16)
        y = bottom - (h + px(12)) * (slot + 1)
        self.geometry(f"{w}x{h}+{x}+{max(0, y)}")
        self.fade(0.0)
        self.after(20000, self.close)
        with app.store.lock:
            sound = app.store.data["settings"].get("sound", True)
        if sound:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def fade(self, a):
        a = min(1.0, a + 0.12)
        try:
            self.attributes("-alpha", a)
        except tk.TclError:
            return
        if a < 1.0:
            self.after(16, self.fade, a)

    def go_back(self):
        bring_to_front(self.app.tracker.hwnds.get(self.exe))
        self.close()

    def snooze(self):
        self.app.tracker.snooze_until[self.exe] = time.time() + SNOOZE
        self.close()

    def close(self):
        if self.app.popups.get(self.exe) is self:
            del self.app.popups[self.exe]
        try:
            self.destroy()
        except tk.TclError:
            pass


# ==========================================================================
# Main window
# ==========================================================================
class App:
    PAGES = [
        ("dashboard", "", "Dashboard"),
        ("apps", "", "Apps"),
        ("history", "", "History"),
        ("watched", "", "Watched apps"),
        ("settings", "", "Settings"),
    ]

    def __init__(self, root, store, tracker, notify_queue, start_minimized):
        self.root, self.store, self.tracker, self.notify_queue = root, store, tracker, notify_queue
        self.popups, self.icons = {}, {}
        self.apps_day = None          # None = today
        self.h_hover = self.d_hover = None
        self.d_hours = [0.0] * 24
        self.h_vals, self.h_dates, self.h_slot = [], [], 0
        self.current = "dashboard"

        root.title(APP_NAME)
        root.geometry(f"{px(1120)}x{px(760)}")
        root.minsize(px(960), px(620))
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.setup_style()
        self.set_window_icon()
        dark_title_bar(root)

        side = tk.Frame(root, bg=SIDEBAR, width=px(232))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.content = tk.Frame(root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.build_sidebar(side)

        self.pages = {}
        for key, _, _ in self.PAGES:
            sf = ScrollFrame(self.content)
            sf.place(x=0, y=0, relwidth=1, relheight=1)
            getattr(self, f"build_{key}")(sf.inner)
            self.pages[key] = sf
        root.bind_all("<MouseWheel>", self.on_wheel)

        self.show("dashboard")
        self.root.after(1000, self.tick)
        self.poll_notifications()
        if start_minimized:
            root.iconify()

    # ---------------- setup ----------------
    def setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TCombobox", fieldbackground=CARD_HI, background=CARD_HI, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=BORDER, lightcolor=CARD_HI, darkcolor=CARD_HI,
                        selectbackground=CARD_HI, selectforeground=TEXT, insertcolor=TEXT, padding=px(6))
        style.map("Dark.TCombobox", fieldbackground=[("readonly", CARD_HI)],
                  foreground=[("readonly", TEXT)], background=[("active", "#30364a")])
        self.root.option_add("*TCombobox*Listbox.background", CARD_HI)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#0d1020")
        self.root.option_add("*TCombobox*Listbox.font", F(10))
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    def set_window_icon(self):
        n = 64
        img = tk.PhotoImage(width=n, height=n)
        for y in range(n):
            for x in range(n):
                dx, dy = x - 31.5, y - 31.5
                if dx * dx + dy * dy > 30 * 30:
                    continue
                hand = (abs(dx) <= 3 and -21 <= dy <= 1) or (abs(dy) <= 3 and -1 <= dx <= 15)
                img.put("#ffffff" if hand else ACCENT, (x, y))
        self.window_icon = img
        self.root.iconphoto(True, img)

    def icon_for(self, exe, size):
        key = (exe, size)
        if key in self.icons:
            return self.icons[key]
        with self.store.lock:
            path = self.store.data["paths"].get(exe)
        if not path:
            return None
        img = None
        try:
            ppm = icon_ppm(path, size, CARD)
            if ppm:
                img = tk.PhotoImage(data=ppm, format="PPM")
        except Exception:
            log_error()
        self.icons[key] = img
        return img

    # ---------------- sidebar ----------------
    def build_sidebar(self, side):
        logo = tk.Frame(side, bg=SIDEBAR)
        logo.pack(fill="x", padx=px(22), pady=(px(26), px(28)))
        tk.Label(logo, text="", font=(ICON_FONT, 18), bg=SIDEBAR, fg=ACCENT).pack(side="left")
        tk.Label(logo, text="App Timer", font=F(14, "bold"), bg=SIDEBAR, fg=TEXT).pack(side="left", padx=px(10))

        self.nav = {}
        for key, glyph, text in self.PAGES:
            item = NavItem(side, glyph, text, lambda k=key: self.show(k))
            item.pack(fill="x")
            self.nav[key] = item

        bottom = tk.Frame(side, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=px(18), pady=px(20))
        box = tk.Frame(bottom, bg=CARD, padx=px(14), pady=px(12), highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(fill="x")
        top = tk.Frame(box, bg=CARD)
        top.pack(fill="x")
        self.s_dot = tk.Canvas(top, width=px(10), height=px(10), bg=CARD, highlightthickness=0)
        self.s_dot.pack(side="left", padx=(0, px(8)))
        self.s_title = tk.Label(top, bg=CARD, fg=TEXT, font=F(10, "bold"))
        self.s_title.pack(side="left")
        self.s_sub = tk.Label(box, bg=CARD, fg=MUTED, font=F(8), anchor="w", justify="left")
        self.s_sub.pack(anchor="w", pady=(px(3), 0))
        FlatButton(bottom, "Quit App Timer", self.quit, kind="side", font=F(9)).pack(fill="x", pady=(px(10), 0))

    def show(self, key):
        self.current = key
        for k, item in self.nav.items():
            item.set_active(k == key)
        self.pages[key].tkraise()
        self.pages[key].canvas.yview_moveto(0)
        self.refresh()

    def on_wheel(self, e):
        if isinstance(e.widget, str):
            return
        try:
            if e.widget.winfo_toplevel() is not self.root or e.widget.winfo_class() == "Listbox":
                return
        except Exception:
            return
        self.pages[self.current].scroll(e.delta)

    # ---------------- dashboard ----------------
    def build_dashboard(self, p):
        _, self.d_greet, self.d_date = page_title(p, "")

        row = tk.Frame(p, bg=BG)
        row.pack(fill="x")
        for i in range(3):
            row.columnconfigure(i, weight=1, uniform="stat")

        c1 = card(row)
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, px(8)))
        caption(c1, "Screen time today")
        self.d_screen = tk.Label(c1, bg=CARD, fg=TEXT, font=F(26, "bold"))
        self.d_screen.pack(anchor="w", pady=(px(4), 0))
        self.d_screen_sub = tk.Label(c1, bg=CARD, fg=MUTED, font=F(9))
        self.d_screen_sub.pack(anchor="w")

        c2 = card(row)
        c2.grid(row=0, column=1, sticky="nsew", padx=px(8))
        caption(c2, "Most used today")
        self.d_top_icon, self.d_top_name, self.d_top_sub = self.stat_app(c2)

        c3 = card(row)
        c3.grid(row=0, column=2, sticky="nsew", padx=(px(8), 0))
        caption(c3, "In front right now")
        self.d_now_icon, self.d_now_name, self.d_now_sub = self.stat_app(c3)

        c4 = card(p)
        c4.pack(fill="x", pady=(px(16), 0))
        card_header(c4, "Activity today", lambda r: tk.Label(r, text="screen time per hour  ·  hover for details",
                                                             bg=CARD, fg=MUTED, font=F(9)))
        self.hour_chart = tk.Canvas(c4, height=px(140), bg=CARD, highlightthickness=0)
        self.hour_chart.pack(fill="x")
        self.hour_chart.bind("<Configure>", lambda e: self.draw_hours())
        self.hour_chart.bind("<Motion>", lambda e: self.chart_hover("d", e.x))
        self.hour_chart.bind("<Leave>", lambda e: self.chart_hover("d", None))

        c5 = card(p)
        c5.pack(fill="x", pady=(px(16), 0))
        card_header(c5, "Top apps today", lambda r: FlatButton(r, "See all  ›", lambda: self.show("apps"),
                                                                kind="subtle", font=F(9, "bold")))
        lst = tk.Frame(c5, bg=CARD)
        lst.pack(fill="x")
        self.d_rows = RowList(lst, lambda parent, k: AppRow(parent, self, k))
        self.d_empty = tk.Label(c5, bg=CARD, fg=MUTED, font=F(10),
                                text="Nothing yet. Use your laptop for a bit and your apps will show up here.")

    def stat_app(self, parent):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(px(10), 0))
        icon = AppIcon(row, self, None, size=40)
        icon.pack(side="left")
        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", padx=px(12))
        name = tk.Label(txt, bg=CARD, fg=TEXT, font=F(12, "bold"), anchor="w")
        name.pack(anchor="w")
        sub = tk.Label(txt, bg=CARD, fg=MUTED, font=F(9), anchor="w")
        sub.pack(anchor="w")
        return icon, name, sub

    def refresh_dashboard(self):
        today = dt.date.today()
        with self.store.lock:
            days = self.store.data["days"]
            day = days.get(today.isoformat(), {})
            screen = day.get("screen", 0)
            hours = list(day.get("hours", [0.0] * 24))
            apps = {e: dict(r) for e, r in day.get("apps", {}).items()}
            yesterday = days.get((today - dt.timedelta(days=1)).isoformat(), {}).get("screen", 0)
            watched = set(self.store.data["watched"])
            names = {e: self.store.name(e) for e in apps}
        self.d_greet.configure(text=greeting())
        self.d_date.configure(text=dt.datetime.now().strftime("%A, %d %B"))
        self.d_screen.configure(text=fmt(screen))
        self.d_screen_sub.configure(text=f"Yesterday: {fmt(yesterday)}" if yesterday else "Laptop awake & unlocked")

        order = sorted(apps, key=lambda e: apps[e]["counted"], reverse=True)
        if order:
            top = order[0]
            self.d_top_icon.set(top)
            self.d_top_name.configure(text=short(names[top], 20))
            self.d_top_sub.configure(text=fmt(apps[top]["counted"]))
        else:
            self.d_top_icon.set(None)
            self.d_top_name.configure(text="—")
            self.d_top_sub.configure(text="No apps yet")

        fg = self.tracker.fg_now
        if self.tracker.locked:
            self.d_now_icon.set(None)
            self.d_now_name.configure(text="Locked")
            self.d_now_sub.configure(text="Tracking is paused", fg=WATCH)
        elif fg:
            self.d_now_icon.set(fg)
            self.d_now_name.configure(text=short(self.store.name(fg), 20))
            in_front = apps.get(fg, {}).get("active", 0)
            self.d_now_sub.configure(text=f"{fmt(in_front)} in front today", fg=GOOD)
        else:
            self.d_now_icon.set(None)
            self.d_now_name.configure(text="App Timer")
            self.d_now_sub.configure(text="You're looking at it", fg=MUTED)

        self.d_hours = hours
        self.draw_hours()

        top6 = order[:6]
        mx = max([apps[e]["counted"] for e in top6] + [1])
        items = []
        for e in top6:
            status, color = self.live_status(e, True)
            items.append((e, dict(name=names[e], status=status, status_color=color, watched=e in watched,
                                  frac=apps[e]["counted"] / mx, time_text=fmt(apps[e]["counted"]))))
        self.d_rows.sync(items)
        if items:
            self.d_empty.pack_forget()
        else:
            self.d_empty.pack(anchor="w")

    def live_status(self, exe, is_today):
        if not is_today:
            return "", MUTED
        if exe == self.tracker.fg_now and not self.tracker.locked:
            return "● In front", GOOD
        if exe in self.tracker.open_now:
            return "● Open in background", MUTED
        return "Closed", DIM

    def chart_hover(self, which, x):
        if which == "d":
            self.d_hover = x
            self.draw_hours()
        else:
            self.h_hover = x
            self.draw_history_chart()

    def draw_hours(self):
        c = self.hour_chart
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        hours = self.d_hours
        if w < 50:
            return
        pad_t, pad_b = px(24), px(22)
        slot = w / 24
        bw = slot * 0.62
        full = h - pad_t - pad_b
        now_h = dt.datetime.now().hour
        hover = int(self.d_hover // slot) if self.d_hover is not None else None
        for i, v in enumerate(hours):
            x0 = i * slot + (slot - bw) / 2
            round_rect(c, x0, pad_t, x0 + bw, h - pad_b, px(4), fill="#20253a")
            bh = max(px(3), full * min(v, 3600) / 3600) if v >= 1 else 0
            if bh:
                color = ACCENT if i == now_h or i == hover else ACCENT_DIM
                round_rect(c, x0, h - pad_b - bh, x0 + bw, h - pad_b, px(4), fill=color)
            if i % 3 == 0:
                c.create_text(x0 + bw / 2, h - px(8), text=f"{i:02d}:00", fill=DIM, font=F(8))
        if hover is not None and 0 <= hover < 24:
            label = f"{hover:02d}:00 – {(hover + 1) % 24:02d}:00    {fmt(hours[hover])}"
            c.create_text(w, px(8), text=label, anchor="e", fill=TEXT, font=F(9, "bold"))

    # ---------------- apps ----------------
    def build_apps(self, p):
        head, _, self.a_sub = page_title(p, "Apps")
        nav = tk.Frame(head, bg=BG)
        nav.place(relx=1.0, rely=0.0, anchor="ne")
        FlatButton(nav, "‹", lambda: self.shift_day(-1), font=F(12, "bold"), padx=12, pady=2).pack(side="left")
        self.a_day = tk.Label(nav, bg=BG, fg=TEXT, font=F(10, "bold"), width=16)
        self.a_day.pack(side="left")
        self.a_next = FlatButton(nav, "›", lambda: self.shift_day(1), font=F(12, "bold"), padx=12, pady=2)
        self.a_next.pack(side="left")

        c = card(p)
        c.pack(fill="x")
        hdr = tk.Frame(c, bg=CARD)
        hdr.pack(fill="x", pady=(0, px(6)))
        app_columns(hdr, True)
        for col, text, sticky in ((1, "APP", "w"), (2, "TIME COUNTED", "w"), (4, "IN FRONT", "e"),
                                  (5, "OPEN", "e"), (6, "WATCH", "e")):
            tk.Label(hdr, text=text, bg=CARD, fg=DIM, font=F(8, "bold")).grid(
                row=0, column=col, sticky=sticky, padx=(px(16) if col == 2 else 0, 0))
        tk.Frame(c, bg=BORDER, height=1).pack(fill="x", pady=(0, px(4)))
        lst = tk.Frame(c, bg=CARD)
        lst.pack(fill="x")
        self.a_rows = RowList(lst, lambda parent, k: AppRow(parent, self, k, full=True))
        self.a_empty = tk.Label(c, bg=CARD, fg=MUTED, font=F(10), text="No app time recorded for this day.")
        tk.Label(p, bg=BG, fg=DIM, font=F(9), justify="left", anchor="w", text=(
            "Time counted = what goes toward your usage.  Normal apps count while open (front or background).  "
            "Watched apps count only while in front.")).pack(anchor="w", pady=(px(12), 0))

    def shift_day(self, d):
        cur = dt.date.fromisoformat(self.apps_day) if self.apps_day else dt.date.today()
        new = cur + dt.timedelta(days=d)
        self.apps_day = None if new >= dt.date.today() else new.isoformat()
        self.refresh()

    def refresh_apps(self):
        today = dt.date.today().isoformat()
        key = self.apps_day or today
        is_today = key == today
        with self.store.lock:
            day = self.store.data["days"].get(key, {})
            screen = day.get("screen", 0)
            apps = {e: dict(r) for e, r in day.get("apps", {}).items()}
            watched = set(self.store.data["watched"])
            names = {e: self.store.name(e) for e in apps}
        d = dt.date.fromisoformat(key)
        if is_today:
            label = "Today"
        elif (dt.date.today() - d).days == 1:
            label = "Yesterday"
        else:
            label = d.strftime("%a, %d %b")
        self.a_day.configure(text=label)
        self.a_next.configure(fg=DIM if is_today else TEXT)
        self.a_sub.configure(text=f"{fmt(screen)} screen time  ·  {len(apps)} apps")
        order = sorted(apps, key=lambda e: apps[e]["counted"], reverse=True)
        mx = max([apps[e]["counted"] for e in order] + [1])
        items = []
        for e in order:
            r = apps[e]
            status, color = self.live_status(e, is_today)
            items.append((e, dict(name=names[e], status=status, status_color=color, watched=e in watched,
                                  frac=r["counted"] / mx, time_text=fmt(r["counted"]),
                                  active=fmt(r["active"]), open_=fmt(r["open"]))))
        self.a_rows.sync(items)
        if items:
            self.a_empty.pack_forget()
        else:
            self.a_empty.pack(anchor="w", pady=px(8))

    def toggle_watch(self, exe):
        with self.store.lock:
            if exe in self.store.data["watched"]:
                del self.store.data["watched"][exe]
            else:
                self.store.data["watched"][exe] = {"remind_after": DEFAULT_REMIND}
        self.store.save()
        self.refresh()

    # ---------------- history ----------------
    def build_history(self, p):
        page_title(p, "History", "Click a day in the chart to see which apps you used.")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x")
        for i in range(3):
            row.columnconfigure(i, weight=1, uniform="stat")
        self.h_stats = []
        for i, title in enumerate(("Daily average · 7 days", "Total · 7 days", "Busiest day · 7 days")):
            c = card(row)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else px(8), 0 if i == 2 else px(8)))
            caption(c, title)
            big = tk.Label(c, bg=CARD, fg=TEXT, font=F(22, "bold"))
            big.pack(anchor="w", pady=(px(4), 0))
            sub = tk.Label(c, bg=CARD, fg=MUTED, font=F(9))
            sub.pack(anchor="w")
            self.h_stats.append((big, sub))

        c = card(p)
        c.pack(fill="x", pady=(px(16), 0))
        card_header(c, "Screen time · last 14 days")
        self.h_chart = tk.Canvas(c, height=px(230), bg=CARD, highlightthickness=0, cursor="hand2")
        self.h_chart.pack(fill="x")
        self.h_chart.bind("<Configure>", lambda e: self.draw_history_chart())
        self.h_chart.bind("<Motion>", lambda e: self.chart_hover("h", e.x))
        self.h_chart.bind("<Leave>", lambda e: self.chart_hover("h", None))
        self.h_chart.bind("<Button-1>", self.open_chart_day)

        c = card(p)
        c.pack(fill="x", pady=(px(16), 0))
        card_header(c, "Top apps · last 7 days")
        lst = tk.Frame(c, bg=CARD)
        lst.pack(fill="x")
        self.h_rows = RowList(lst, lambda parent, k: AppRow(parent, self, k))
        self.h_empty = tk.Label(c, bg=CARD, fg=MUTED, font=F(10), text="No app time recorded yet.")

    def refresh_history(self):
        today = dt.date.today()
        self.h_dates = [today - dt.timedelta(days=i) for i in range(13, -1, -1)]
        totals = {}
        with self.store.lock:
            days = self.store.data["days"]
            self.h_vals = [days.get(d.isoformat(), {}).get("screen", 0) for d in self.h_dates]
            for d in self.h_dates[-7:]:
                for e, r in days.get(d.isoformat(), {}).get("apps", {}).items():
                    totals[e] = totals.get(e, 0) + r["counted"]
            watched = set(self.store.data["watched"])
            names = {e: self.store.name(e) for e in totals}
        week = self.h_vals[-7:]
        tracked = [v for v in week if v > 0]
        total = sum(week)
        avg = total / len(tracked) if tracked else 0
        best = max(range(7), key=lambda i: week[i])
        (b0, s0), (b1, s1), (b2, s2) = self.h_stats
        b0.configure(text=fmt(avg))
        s0.configure(text=f"over {len(tracked)} tracked day{'' if len(tracked) == 1 else 's'}")
        b1.configure(text=fmt(total))
        s1.configure(text="laptop screen time")
        b2.configure(text=self.h_dates[-7 + best].strftime("%A") if total else "—")
        s2.configure(text=fmt(week[best]) if total else "No data yet")
        self.draw_history_chart()

        order = sorted(totals, key=lambda e: totals[e], reverse=True)[:10]
        mx = max([totals[e] for e in order] + [1])
        self.h_rows.sync([(e, dict(name=names[e], status=f"avg {fmt(totals[e] / 7)} / day", status_color=MUTED,
                                   watched=e in watched, frac=totals[e] / mx, time_text=fmt(totals[e])))
                          for e in order])
        if order:
            self.h_empty.pack_forget()
        else:
            self.h_empty.pack(anchor="w")

    def draw_history_chart(self):
        c = self.h_chart
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        vals = self.h_vals
        if w < 50 or not vals:
            return
        pad_t, pad_b = px(26), px(42)
        slot = w / len(vals)
        bw = min(slot * 0.58, px(44))
        top = max(max(vals), 3600)
        full = h - pad_t - pad_b
        hover = int(self.h_hover // slot) if self.h_hover is not None else None
        self.h_slot = slot
        for i, (d, v) in enumerate(zip(self.h_dates, vals)):
            x0 = i * slot + (slot - bw) / 2
            cx = x0 + bw / 2
            is_today = i == len(vals) - 1
            round_rect(c, x0, pad_t, x0 + bw, h - pad_b, px(6), fill="#20253a")
            if v > 0:
                bh = max(px(4), full * v / top)
                color = ACCENT if is_today or i == hover else ACCENT_DIM
                round_rect(c, x0, h - pad_b - bh, x0 + bw, h - pad_b, px(6), fill=color)
                c.create_text(cx, h - pad_b - bh - px(10), text=f"{v / 3600:.1f}h",
                              fill=TEXT if i == hover or is_today else MUTED, font=F(8, "bold"))
            c.create_text(cx, h - pad_b + px(14), text="Today" if is_today else d.strftime("%a"),
                          fill=TEXT if is_today else MUTED, font=F(8, "bold" if is_today else "normal"))
            c.create_text(cx, h - pad_b + px(29), text=d.strftime("%d %b"), fill=DIM, font=F(7))

    def open_chart_day(self, e):
        if not self.h_slot:
            return
        i = int(e.x // self.h_slot)
        if 0 <= i < len(self.h_dates):
            d = self.h_dates[i]
            self.apps_day = None if d == dt.date.today() else d.isoformat()
            self.show("apps")

    # ---------------- watched ----------------
    def build_watched(self, p):
        page_title(p, "Watched apps",
                   "A watched app only counts time while it's in front. If you leave it in the background "
                   "(minimized or behind another window), its timer pauses and you get a reminder to go back. "
                   "Apps you don't watch count all the time they're open.")
        c = card(p)
        c.pack(fill="x")
        card_header(c, "Add an app")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x")
        self.w_app = tk.StringVar()
        self.w_box = ttk.Combobox(row, textvariable=self.w_app, style="Dark.TCombobox", font=F(10),
                                  postcommand=self.fill_app_choices)
        self.w_box.pack(side="left", fill="x", expand=True)
        FlatButton(row, "Add", self.add_watch_from_form, kind="primary").pack(side="right")
        self.w_step = Stepper(row, 1, lambda m: None)
        self.w_step.pack(side="right", padx=px(14))
        tk.Label(row, bg=CARD, fg=MUTED, font=F(9), text="Remind after").pack(side="right", padx=(px(16), 0))
        tk.Label(c, bg=CARD, fg=DIM, font=F(9), anchor="w",
                 text="Pick from the list (apps open now come first) or type a program name like chrome.exe"
                 ).pack(anchor="w", pady=(px(8), 0))

        c = card(p)
        c.pack(fill="x", pady=(px(16), 0))
        card_header(c, "Your watched apps")
        lst = tk.Frame(c, bg=CARD)
        lst.pack(fill="x")
        self.w_rows = RowList(lst, lambda parent, k: WatchRow(parent, self, k))
        self.w_empty = tk.Label(c, bg=CARD, fg=MUTED, font=F(10), anchor="w", justify="left",
                                text="You aren't watching any apps yet.\nAdd one above, or click “Watch” "
                                     "next to an app on the Apps page.")

    def fill_app_choices(self):
        with self.store.lock:
            known = set(self.store.data["names"])
            for day in self.store.data["days"].values():
                known.update(day["apps"])
            known -= set(self.store.data["watched"])
            items = sorted(known, key=lambda e: (e not in self.tracker.open_now, self.store.name(e).lower()))
            self.w_box.configure(values=[f"{self.store.name(e)}  —  {e}" for e in items])

    def add_watch_from_form(self):
        text = self.w_app.get().strip()
        if "—" in text:
            text = text.rsplit("—", 1)[1].strip()
        exe = text.lower()
        if not exe:
            messagebox.showinfo(APP_NAME, "Choose or type an app first.")
            return
        if not exe.endswith(".exe"):
            exe += ".exe"
        with self.store.lock:
            self.store.data["watched"][exe] = {"remind_after": self.w_step.value * 60}
        self.store.save()
        self.w_app.set("")
        self.refresh()

    def set_remind(self, exe, minutes):
        with self.store.lock:
            if exe in self.store.data["watched"]:
                self.store.data["watched"][exe]["remind_after"] = minutes * 60
        self.store.save()

    def remove_watch(self, exe):
        with self.store.lock:
            self.store.data["watched"].pop(exe, None)
        self.store.save()
        self.refresh()

    def refresh_watched(self):
        with self.store.lock:
            watched = {e: dict(c) for e, c in self.store.data["watched"].items()}
            names = {e: self.store.name(e) for e in watched}
        away = dict(self.tracker.away)
        items = []
        for e in sorted(watched, key=lambda x: names[x].lower()):
            if self.tracker.locked and e in self.tracker.open_now:
                status, color = "Laptop locked, paused", MUTED
            elif e == self.tracker.fg_now:
                status, color = "In front, counting", GOOD
            elif e in away:
                status, color = f"In background for {fmt(time.time() - away[e]['since'])}, paused", WATCH
            elif e in self.tracker.open_now:
                status, color = "In background, paused", WATCH
            else:
                status, color = "Not open", DIM
            if time.time() < self.tracker.snooze_until.get(e, 0):
                status += "  ·  snoozed"
            minutes = watched[e].get("remind_after", DEFAULT_REMIND) / 60
            items.append((e, dict(name=names[e], status=status, color=color, minutes=minutes)))
        self.w_rows.sync(items)
        if items:
            self.w_empty.pack_forget()
        else:
            self.w_empty.pack(anchor="w")

    # ---------------- settings ----------------
    def build_settings(self, p):
        page_title(p, "Settings")
        c = card(p)
        c.pack(fill="x")
        with self.store.lock:
            st = dict(self.store.data["settings"])
        self.t_startup = self.setting_row(c, "Start with Windows",
                                          "Open App Timer automatically (minimized) when you sign in.",
                                          startup_enabled(), self.toggle_startup, first=True)
        self.setting_row(c, "Reminders for watched apps",
                         "Show a popup when a watched app stays in the background too long.",
                         st.get("reminders", True), lambda v: self.set_setting("reminders", v))
        self.setting_row(c, "Reminder sound", "Play a sound with each reminder.",
                         st.get("sound", True), lambda v: self.set_setting("sound", v))

        c = card(p)
        c.pack(fill="x", pady=(px(16), 0))
        card_header(c, "How time is counted")
        for line in ("Screen time counts while the laptop is awake and unlocked. Sleep, the lock screen "
                     "and shutdown don't count.",
                     "Normal apps count the whole time they're open, in front or in the background.",
                     "Watched apps count only while they're in front.",
                     "Closing this window keeps App Timer running in the background. Use Quit in the "
                     "sidebar to stop it."):
            r = tk.Frame(c, bg=CARD)
            r.pack(fill="x", pady=px(3))
            tk.Label(r, text="•", bg=CARD, fg=ACCENT, font=F(10, "bold")).pack(side="left", anchor="n")
            tk.Label(r, text=line, bg=CARD, fg=MUTED, font=F(10), justify="left", anchor="w",
                     wraplength=px(720)).pack(side="left", padx=px(8))

        c = card(p)
        c.pack(fill="x", pady=(px(16), 0))
        card_header(c, "Your data", lambda r: FlatButton(r, "Open folder", lambda: os.startfile(DATA_DIR),
                                                         font=F(9, "bold"), padx=12, pady=4))
        tk.Label(c, text=DATA_FILE, bg=CARD, fg=MUTED, font=F(9), anchor="w").pack(anchor="w")

    def setting_row(self, parent, title, desc, value, command, first=False):
        if not first:
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=px(14))
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x")
        tog = Toggle(row, value, command)
        tog.pack(side="right")
        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=title, bg=CARD, fg=TEXT, font=F(10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(txt, text=desc, bg=CARD, fg=MUTED, font=F(9), anchor="w").pack(anchor="w")
        return tog

    def toggle_startup(self, value):
        try:
            set_startup(value)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not change the startup setting:\n{e}")
            self.t_startup.set(startup_enabled())

    def set_setting(self, key, value):
        with self.store.lock:
            self.store.data["settings"][key] = value
        self.store.save()

    # ---------------- loop ----------------
    def tick(self):
        self.refresh()
        self.root.after(1000, self.tick)

    def refresh(self):
        try:
            getattr(self, f"refresh_{self.current}", lambda: None)()
            self.refresh_status()
        except Exception:
            log_error()

    def refresh_status(self):
        with self.store.lock:
            screen = self.store.data["days"].get(dt.date.today().isoformat(), {}).get("screen", 0)
        locked = self.tracker.locked
        self.s_dot.delete("all")
        self.s_dot.create_oval(0, 0, px(10), px(10), fill=WATCH if locked else GOOD, outline="")
        self.s_title.configure(text="Paused" if locked else "Tracking")
        self.s_sub.configure(text="Laptop is locked" if locked else f"{fmt(screen)} screen time today")

    def poll_notifications(self):
        try:
            while True:
                exe, away = self.notify_queue.get_nowait()
                old = self.popups.pop(exe, None)
                if old:
                    old.destroy()
                used = {p.slot for p in self.popups.values()}
                slot = 0
                while slot in used:
                    slot += 1
                self.popups[exe] = Reminder(self, exe, away, slot)
        except queue.Empty:
            pass
        except Exception:
            log_error()
        self.root.after(500, self.poll_notifications)

    def on_close(self):
        with self.store.lock:
            shown = self.store.data["settings"].get("close_hint_shown")
            self.store.data["settings"]["close_hint_shown"] = True
        if not shown:
            messagebox.showinfo(APP_NAME, "App Timer keeps tracking in the background.\n\n"
                                          "It's minimized to the taskbar. To stop it completely, "
                                          "use Quit in the sidebar.")
        self.root.iconify()

    def quit(self):
        if not messagebox.askyesno(APP_NAME, "Stop tracking and close App Timer?"):
            return
        self.tracker.stop_event.set()
        self.store.save()
        self.root.destroy()


def main():
    mutex = kernel32.CreateMutexW(None, False, "Local\\AppTimerSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            bring_to_front(hwnd)
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    store = Store()
    notify_queue = queue.Queue()
    tracker = Tracker(store, notify_queue)
    tracker.start()

    root = tk.Tk()
    setup_fonts(root)
    App(root, store, tracker, notify_queue, "--minimized" in sys.argv)
    try:
        root.mainloop()
    finally:
        tracker.stop_event.set()
        store.save()
        del mutex


if __name__ == "__main__":
    main()
