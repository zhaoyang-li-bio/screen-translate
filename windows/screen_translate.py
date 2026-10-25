# -*- coding: utf-8 -*-
"""划词翻译（Windows 桌面软件）

划词检测：WH_MOUSE_LL 低层鼠标钩子（不依赖目标程序是否发选区事件，
          WPS / Word / 浏览器 / PDF 都适用）
取词方式：模拟 Ctrl+C 读剪贴板，读完还原原剪贴板内容
翻译通道：Google 免费接口 / DeepL / OpenAI 兼容接口
界面：tkinter 主窗口 + 光标旁置顶弹窗

快捷键：Alt+F1 暂停/继续   F8 手动翻译当前选中文本   Esc 关闭弹窗
"""

import ctypes
import ctypes.wintypes as w
import atexit
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

# ---------------------------------------------------------------- 配置

APP_NAME = "划词翻译"
MUTEX_NAME = "Global\\ScreenTranslate_SingleInstance"

DEFAULT_CONFIG = {
    "provider": "auto",
    "target_lang": "auto",
    "deepl_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "trigger_drag": True,
    "trigger_double_click": True,
    "popup_timeout_sec": 20,
    "max_text_len": 800,
    "autostart": False,
}


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(base_dir(), "screen_translate.json")
ERROR_LOG = os.path.join(base_dir(), "error.log")


def log_error(text):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + text + "\n")
    except Exception:
        pass


def install_crash_logging(root=None):
    def hook(exc_type, exc, tb):
        log_error("".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = hook

    def thread_hook(args):
        log_error("".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))

    threading.excepthook = thread_hook
    if root is not None:
        root.report_callback_exception = lambda t, v, tb: log_error(
            "".join(traceback.format_exception(t, v, tb)))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
VK_F1 = 0x70
VK_F8 = 0x77
VK_CONTROL = 0x11
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", INPUT_UNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)
WINPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, w.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [w.UINT]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [w.UINT, ctypes.c_void_p]
user32.GetClipboardSequenceNumber.restype = w.DWORD
user32.WindowFromPoint.restype = w.HWND
user32.WindowFromPoint.argtypes = [w.POINT]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def cursor_pos():
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_pid(hwnd):
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_own_window_at(x, y):
    hwnd = user32.WindowFromPoint(w.POINT(x, y))
    return bool(hwnd) and window_pid(hwnd) == os.getpid()


def send_ctrl_c():
    def key(vk, up=False):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0,
                              dwFlags=KEYEVENTF_KEYUP if up else 0,
                              time=0, dwExtraInfo=0)
        return inp

    seq = [key(VK_CONTROL), key(VK_C), key(VK_C, True), key(VK_CONTROL, True)]
    arr = (INPUT * len(seq))(*seq)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def clipboard_seq():
    return user32.GetClipboardSequenceNumber()


def clipboard_get_text():
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text):
    if text is None:
        return
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def grab_selected_text(timeout=0.7):
    """模拟 Ctrl+C 取选中文本，取完还原原剪贴板。取不到返回 None。"""
    old_text = clipboard_get_text()
    old_seq = clipboard_seq()
    send_ctrl_c()

    deadline = time.time() + timeout
    text = None
    while time.time() < deadline:
        time.sleep(0.03)
        if clipboard_seq() != old_seq:
            text = clipboard_get_text()
            break

    if text and text.strip():
        if old_text != text:
            clipboard_set_text(old_text)
        return text
    return None


# ---------------------------------------------------------------- 翻译


def is_cjk(text):
    return len(re.findall(r"[\u4e00-\u9fff]", text)) > len(text) * 0.2


def target_lang_for(text, cfg):
    configured = cfg.get("target_lang", "auto")
    if configured and configured != "auto":
        return configured
    return "en" if is_cjk(text) else "zh-CN"


def translate(text, cfg):
    provider = cfg.get("provider", "auto")
    if provider == "auto":
        last = None
        for fn in (translate_google, translate_mymemory):
            try:
                return fn(text, cfg)
            except Exception as exc:
                last = exc
        raise RuntimeError("所有免费通道都失败了：%s" % last)
    if provider == "google":
        return translate_google(text, cfg)
    if provider == "mymemory":
        return translate_mymemory(text, cfg)
    if provider == "deepl":
        return translate_deepl(text, cfg)
    if provider == "openai":
        return translate_openai(text, cfg)
    raise RuntimeError("未知翻译通道：" + provider)


def http_json(url, timeout=10, retries=2, data=None, headers=None):
    """带重试的 JSON 请求：免费接口偶发握手超时，重试一次通常就恢复。"""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last


def translate_google(text, cfg):
    tl = target_lang_for(text, cfg)
    qs = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text})
    data = http_json("https://translate.googleapis.com/translate_a/single?" + qs)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def translate_mymemory(text, cfg):
    tl = target_lang_for(text, cfg)
    sl = "zh-CN" if tl != "zh-CN" else "en"
    qs = urllib.parse.urlencode({"q": text, "langpair": "%s|%s" % (sl, tl)})
    data = http_json("https://api.mymemory.translated.net/get?" + qs)
    translated = data.get("responseData", {}).get("translatedText")
    if not translated:
        raise RuntimeError("MyMemory 无译文")
    return translated


def translate_deepl(text, cfg):
    tl = target_lang_for(text, cfg)
    key = cfg.get("deepl_api_key", "")
    if not key:
        raise RuntimeError("未填写 DeepL API Key")
    body = urllib.parse.urlencode({
        "auth_key": key, "text": text,
        "target_lang": "ZH" if tl.startswith("zh") else tl.upper()}).encode()
    data = http_json("https://api-free.deepl.com/v2/translate", data=body)
    return data["translations"][0]["text"]


def translate_openai(text, cfg):
    tl = target_lang_for(text, cfg)
    key = cfg.get("openai_api_key", "")
    if not key:
        raise RuntimeError("未填写 OpenAI API Key")
    payload = json.dumps({
        "model": cfg.get("openai_model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "你是翻译引擎，只输出译文，不要解释。"},
            {"role": "user", "content": "翻译成 %s：\n%s" % (tl, text)},
        ],
        "temperature": 0.2,
    }).encode()
    data = http_json(
        cfg.get("openai_base_url", "").rstrip("/") + "/chat/completions",
        timeout=25,
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return data["choices"][0]["message"]["content"].strip()


def friendly_error(exc):
    msg = str(exc)
    low = msg.lower()
    if ("timed out" in low or "ssl" in low or "eof" in low
            or "connection refused" in low or "temporary failure" in low):
        return "网络异常，连不上翻译服务（检查网络/代理后重试）"
    return msg


PROVIDER_LABELS = [
    ("自动（免费，Google→MyMemory）", "auto"),
    ("Google（免费，无需 Key）", "google"),
    ("MyMemory（免费，无需 Key）", "mymemory"),
    ("DeepL（需 Key）", "deepl"),
    ("OpenAI 兼容（需 Key）", "openai"),
]
LABEL_BY_ID = {pid: label for label, pid in PROVIDER_LABELS}
ID_BY_LABEL = {label: pid for label, pid in PROVIDER_LABELS}


# ---------------------------------------------------------------- 弹窗

BG = "#FFFFFF"
FG = "#111111"
SUB = "#6B7280"
BORDER = "#D1D5DB"


class Popup:
    def __init__(self, root):
        self.root = root
        self.win = None
        self.timer = None

    def close(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.win is not None:
            self.win.destroy()
            self.win = None

    def show(self, x, y, source, result, pending=False, timeout_ms=20000):
        self.close()
        win = tk.Toplevel(self.root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER)

        frame = tk.Frame(win, bg=BG, padx=14, pady=10)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        tk.Label(frame, text=source, bg=BG, fg=SUB, font=("Microsoft YaHei UI", 9),
                 justify="left", wraplength=420, anchor="w").pack(fill="x")

        body = tk.Label(frame, text=result, bg=BG, fg=FG, font=("Microsoft YaHei UI", 11),
                        justify="left", wraplength=420, anchor="w")
        body.pack(fill="x", pady=(8, 0))

        if not pending:
            bar = tk.Frame(frame, bg=BG)
            bar.pack(fill="x", pady=(8, 0))
            copy = tk.Label(bar, text="复制译文", bg=BG, fg="#2563EB",
                            font=("Microsoft YaHei UI", 8), cursor="hand2")
            copy.pack(side="left")
            copy.bind("<Button-1>", lambda e: clipboard_set_text(result))
            tk.Label(bar, text="Esc / 点击关闭", bg=BG, fg="#9CA3AF",
                     font=("Microsoft YaHei UI", 8)).pack(side="right")

        win.update_idletasks()
        ww, wh = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("+%d+%d" % (min(max(0, x + 14), sw - ww - 8),
                                 min(max(0, y + 20), sh - wh - 8)))

        win.bind("<Escape>", lambda e: self.close())
        win.bind("<Button-1>", lambda e: self.close())

        if not pending:
            self.timer = self.root.after(timeout_ms, self.close)


# ---------------------------------------------------------------- 鼠标钩子


class MouseWatcher(threading.Thread):
    """低层鼠标钩子 + 全局热键线程，识别拖选 / 双击选词。"""

    def __init__(self, events, app):
        super().__init__(daemon=True)
        self.events = events
        self.app = app
        self._hook_proc = None
        self._down = None      # (x, y, t)
        self._dragged = False
        self._last_click = 0.0
        self._last_click_pos = (0, 0)

    def run(self):
        self._hook_proc = HOOKPROC(self._on_mouse)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, None, 0)
        if not self._hook:
            self.events.put(("log", "鼠标钩子安装失败，错误码 %s" % ctypes.get_last_error()))
            return

        hk1 = user32.RegisterHotKey(None, 1, MOD_ALT, VK_F1)
        hk2 = user32.RegisterHotKey(None, 2, 0, VK_F8)
        if not hk1:
            self.events.put(("log", "Alt+F1 注册失败（可能被其它软件占用）"))
        if not hk2:
            self.events.put(("log", "F8 注册失败（可能被其它软件占用）"))
        self.events.put(("log", "鼠标钩子已就绪，可以在其它软件里拖选文字了"))

        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == 1:
                    self.events.put(("toggle",))
                elif msg.wParam == 2:
                    self.events.put(("manual",))
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _on_mouse(self, code, wparam, lparam):
        try:
            if code >= 0:
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                if wparam == WM_LBUTTONDOWN:
                    self._down = (x, y, time.time())
                    self._dragged = False
                elif wparam == WM_MOUSEMOVE:
                    if self._down:
                        dx = abs(x - self._down[0])
                        dy = abs(y - self._down[1])
                        if dx >= 4 or dy >= 4:
                            self._dragged = True
                elif wparam == WM_LBUTTONUP:
                    self._on_up(x, y)
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_up(self, x, y):
        cfg = self.app.cfg
        down = self._down
        self._down = None
        if not down:
            return
        dx, dy = abs(x - down[0]), abs(y - down[1])
        elapsed = time.time() - down[2]

        drag = self._dragged and (dx >= 4 or dy >= 4) and elapsed <= 3.0
        now = time.time()
        dbl = (dx <= 6 and dy <= 6 and now - self._last_click <= 0.4
               and abs(x - self._last_click_pos[0]) <= 6
               and abs(y - self._last_click_pos[1]) <= 6)
        if dx <= 6 and dy <= 6:
            self._last_click = now
            self._last_click_pos = (x, y)

        if not (cfg.get("trigger_drag", True) and drag) and \
           not (cfg.get("trigger_double_click", True) and dbl):
            return
        if is_own_window_at(x, y):
            return
        self.events.put(("select", x, y))


# ---------------------------------------------------------------- 主程序


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = queue.Queue()
        self.enabled = True
        self.busy = threading.Lock()
        self.last_trigger = 0.0

        self.root = tk.Tk()
        install_crash_logging(self.root)
        self.root.title(APP_NAME)
        self.root.geometry("560x480")
        self.root.minsize(520, 440)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.popup = Popup(self.root)
        self._build_ui()
        MouseWatcher(self.events, self).start()
        self.root.after(80, self._poll)

    # -- UI ------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        top = tk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="运行中")
        self.status_label = tk.Label(top, textvariable=self.status_var,
                                     fg="#059669", font=("Microsoft YaHei UI", 12, "bold"))
        self.status_label.pack(side="left")
        self.toggle_btn = tk.Button(top, text="暂停划词", width=10, command=self.toggle)
        self.toggle_btn.pack(side="right")
        tk.Label(top, text="Alt+F1 切换 / F8 手动翻译", fg="#6B7280",
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=8)

        box = ttk.LabelFrame(self.root, text="设置")
        box.pack(fill="x", **pad)

        r1 = tk.Frame(box)
        r1.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(r1, text="翻译通道").pack(side="left")
        self.provider = ttk.Combobox(r1, state="readonly", width=30,
                                     values=[label for label, _ in PROVIDER_LABELS])
        self.provider.set(LABEL_BY_ID.get(self.cfg.get("provider", "auto"),
                                          PROVIDER_LABELS[0][0]))
        self.provider.pack(side="left", padx=6)

        r1b = tk.Frame(box)
        r1b.pack(fill="x", padx=10, pady=4)
        tk.Label(r1b, text="目标语言").pack(side="left")
        self.lang = ttk.Combobox(r1b, state="readonly", width=10,
                                 values=["auto", "zh-CN", "en", "ja", "ko"])
        self.lang.set(self.cfg.get("target_lang", "auto"))
        self.lang.pack(side="left", padx=6)
        tk.Label(r1b, text="弹窗秒数").pack(side="left", padx=(16, 0))
        self.timeout = tk.Spinbox(r1b, from_=3, to=120, width=4)
        self.timeout.delete(0, "end")
        self.timeout.insert(0, str(self.cfg.get("popup_timeout_sec", 20)))
        self.timeout.pack(side="left", padx=6)

        r2 = tk.Frame(box)
        r2.pack(fill="x", padx=10, pady=4)
        self.drag_var = tk.BooleanVar(value=self.cfg.get("trigger_drag", True))
        self.dbl_var = tk.BooleanVar(value=self.cfg.get("trigger_double_click", True))
        tk.Checkbutton(r2, text="拖选触发", variable=self.drag_var).pack(side="left")
        tk.Checkbutton(r2, text="双击选词触发", variable=self.dbl_var).pack(side="left", padx=10)

        r3 = tk.Frame(box)
        r3.pack(fill="x", padx=10, pady=4)
        tk.Label(r3, text="API Key（免费通道留空）").pack(side="left")
        self.key_entry = tk.Entry(r3, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.key_entry.insert(0, self.cfg.get("deepl_api_key") or self.cfg.get("openai_api_key") or "")

        r4 = tk.Frame(box)
        r4.pack(fill="x", padx=10, pady=(4, 10))
        self.autostart_var = tk.BooleanVar(value=self.cfg.get("autostart", False))
        tk.Checkbutton(r4, text="开机自启", variable=self.autostart_var).pack(side="left")
        tk.Button(r4, text="保存设置", command=self.save).pack(side="right")

        logbox = ttk.LabelFrame(self.root, text="运行日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, height=8, state="disabled", wrap="word",
                           font=("Microsoft YaHei UI", 9), bg="#FAFAFA", relief="flat")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(self.root, text="提示：窗口可以最小化，划词在后台继续工作。",
                 fg="#9CA3AF", font=("Microsoft YaHei UI", 8)).pack(pady=(0, 8))

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- 行为 ----------------------------------------------------------

    def toggle(self):
        self.enabled = not self.enabled
        if self.enabled:
            self.status_var.set("运行中")
            self.status_label.configure(fg="#059669")
            self.toggle_btn.configure(text="暂停划词")
        else:
            self.status_var.set("已暂停")
            self.status_label.configure(fg="#D97706")
            self.toggle_btn.configure(text="继续划词")
            self.popup.close()
        self.log_line("划词已" + ("开启" if self.enabled else "暂停"))

    def save(self):
        cfg = self.cfg
        cfg["provider"] = ID_BY_LABEL.get(self.provider.get(), "auto")
        cfg["target_lang"] = self.lang.get()
        cfg["trigger_drag"] = bool(self.drag_var.get())
        cfg["trigger_double_click"] = bool(self.dbl_var.get())
        try:
            cfg["popup_timeout_sec"] = max(3, int(self.timeout.get()))
        except Exception:
            cfg["popup_timeout_sec"] = 20
        key = self.key_entry.get().strip()
        if cfg["provider"] == "deepl":
            cfg["deepl_api_key"] = key
        elif cfg["provider"] == "openai":
            cfg["openai_api_key"] = key
        cfg["autostart"] = bool(self.autostart_var.get())
        try:
            save_config(cfg)
            self._apply_autostart(cfg["autostart"])
            self.log_line("设置已保存")
        except Exception as exc:
            messagebox.showerror(APP_NAME, "保存失败：%s" % exc)

    def _apply_autostart(self, enable):
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                target = sys.executable if getattr(sys, "frozen", False) else \
                    '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, target)
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass

    # -- 取词 + 翻译 ----------------------------------------------------

    def _trigger(self, x, y):
        now = time.time()
        if now - self.last_trigger < 0.45:
            return
        self.last_trigger = now
        threading.Thread(target=self._work, args=(x, y), daemon=True).start()

    def _work(self, x, y):
        if not self.busy.acquire(blocking=False):
            return
        try:
            time.sleep(0.05)
            fg = user32.GetForegroundWindow()
            if fg and window_pid(fg) == os.getpid():
                return  # 焦点在自身窗口，不去抢复制
            text = grab_selected_text()
            if not text:
                return
            text = text.strip()
            if len(text) > self.cfg.get("max_text_len", 800):
                text = text[: self.cfg["max_text_len"]] + "…"
            self.events.put(("pending", x, y, text))
            try:
                result = translate(text, self.cfg)
            except urllib.error.HTTPError as exc:
                result = "翻译失败：HTTP %s" % exc.code
            except Exception as exc:
                result = "翻译失败：%s" % friendly_error(exc)
            self.events.put(("result", x, y, text, result))
            self.events.put(("log", "已翻译：%s" % text[:30]))
        finally:
            self.busy.release()

    # -- 事件循环 --------------------------------------------------------

    def _poll(self):
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "select":
                    if self.enabled:
                        self._trigger(item[1], item[2])
                elif kind == "manual":
                    x, y = cursor_pos()
                    self._trigger(x, y)
                elif kind == "toggle":
                    self.toggle()
                elif kind == "log":
                    self.log_line(item[1])
                elif kind == "pending":
                    self.popup.show(item[1], item[2], item[3], "翻译中…", pending=True)
                elif kind == "result":
                    self.popup.show(item[1], item[2], item[3], item[4],
                                    timeout_ms=int(self.cfg.get("popup_timeout_sec", 20)) * 1000)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def quit(self):
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.log_line("已启动。在 WPS/浏览器/PDF 里拖选文字试试，或按 F8。")
        self.root.mainloop()


def already_running():
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return True
    return False


def main():
    install_crash_logging()
    log_error("start pid=%s frozen=%s" % (os.getpid(), bool(getattr(sys, "frozen", False))))
    atexit.register(lambda: log_error("process exit pid=%s" % os.getpid()))
    if already_running():
        log_error("程序已在运行，忽略本次启动")
        return 1
    try:
        App(load_config()).run()
    except BaseException:
        log_error("fatal:\n" + traceback.format_exc())
        raise
    log_error("mainloop exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
