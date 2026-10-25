#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""划词翻译（Linux / Ubuntu）

划词检测：监听 X11 的 PRIMARY selection（Linux 里"选中即复制"的那块剪贴板），
          无需任何注入或全局钩子，任何程序里选中文字都会被捕获。
取词命令：X11 用 xclip / xsel，Wayland 用 wl-paste --primary
翻译通道：Google 免费接口 / MyMemory 免费接口（都无需 API Key），
          也可选 DeepL / OpenAI 兼容接口（需 Key）
界面：tkinter 主窗口 + 光标旁置顶弹窗

用法：
  ./ScreenTranslate            启动图形界面（默认）
  ./ScreenTranslate --once     读取当前选区并翻译（适合绑定桌面快捷键）
  ./ScreenTranslate --print    读取当前选区并只打印译文（终端用）

依赖：python3-tk，以及 xclip（X11）或 wl-clipboard（Wayland）
  sudo apt install python3-tk xclip wl-clipboard
"""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

import tkinter as tk

try:
    import fcntl
except ImportError:  # 非 Linux 平台导入时降级
    fcntl = None

APP_NAME = "划词翻译"
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "screen-translate",
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
ERROR_LOG = os.path.join(CONFIG_DIR, "error.log")
LOCK_PATH = os.path.join("/tmp", "screen-translate.lock")

DEFAULT_CONFIG = {
    "provider": "auto",
    "target_lang": "auto",
    "deepl_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "watch_selection": True,
    "poll_interval_ms": 500,
    "popup_timeout_sec": 20,
    "max_text_len": 800,
    "autostart": False,
}

PROVIDER_LABELS = [
    ("自动（免费，Google→MyMemory）", "auto"),
    ("Google（免费，无需 Key）", "google"),
    ("MyMemory（免费，无需 Key）", "mymemory"),
    ("DeepL（需 Key）", "deepl"),
    ("OpenAI 兼容（需 Key）", "openai"),
]
LABEL_BY_ID = {pid: label for label, pid in PROVIDER_LABELS}
ID_BY_LABEL = {label: pid for label, pid in PROVIDER_LABELS}


# ---------------------------------------------------------------- 基础


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def log_error(text):
    try:
        ensure_config_dir()
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
        except Exception as exc:
            log_error("读取配置失败：%s" % exc)
    return cfg


def save_config(cfg):
    ensure_config_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def app_command():
    """返回可用于 .desktop / 快捷键的可执行命令。"""
    if getattr(sys, "frozen", False):
        return '"%s"' % os.path.abspath(sys.argv[0])
    return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))


# ---------------------------------------------------------------- 选区读取


def _run(cmd, timeout=2.0):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def detect_backend():
    """返回可用的选区读取方式。"""
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session == "wayland"
    if wayland and shutil.which("wl-paste"):
        return "wl-paste"
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("xsel"):
        return "xsel"
    if shutil.which("wl-paste"):
        return "wl-paste"
    return None


def read_primary(backend):
    if backend == "xclip":
        return _run(["xclip", "-selection", "primary", "-o"])
    if backend == "xsel":
        return _run(["xsel", "--primary", "--output"])
    if backend == "wl-paste":
        return _run(["wl-paste", "--primary", "--no-newline"])
    return None


class PrimaryWatcher(threading.Thread):
    """轮询 PRIMARY selection，发现新的选中文本就上报。"""

    def __init__(self, events, app, backend):
        super().__init__(daemon=True)
        self.events = events
        self.app = app
        self.backend = backend
        self._stop = threading.Event()
        self._last = ""

    def stop(self):
        self._stop.set()

    def run(self):
        # 启动时先把当前选区记为基线，避免一启动就翻译旧内容
        self._last = (read_primary(self.backend) or "").strip()
        while not self._stop.is_set():
            interval = max(0.15, self.app.cfg.get("poll_interval_ms", 500) / 1000.0)
            if self.app.enabled and self.app.cfg.get("watch_selection", True):
                text = read_primary(self.backend)
                if text:
                    text = text.strip()
                    if text and text != self._last and len(text) <= 5000:
                        self._last = text
                        self.events.put(("select", text))
            self._stop.wait(interval)


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


def friendly_error(exc):
    msg = str(exc)
    low = msg.lower()
    if ("timed out" in low or "ssl" in low or "eof" in low
            or "connection refused" in low or "temporary failure" in low):
        return "网络异常，连不上翻译服务（检查网络/代理后重试）"
    return msg


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


# ---------------------------------------------------------------- 弹窗

BG = "#FFFFFF"
FG = "#111111"
SUB = "#6B7280"
BORDER = "#D1D5DB"
FONT_UI = "Noto Sans CJK SC"


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

    def pointer(self):
        try:
            return self.root.winfo_pointerxy()
        except Exception:
            return (200, 200)

    def show(self, source, result, pending=False, timeout_ms=20000):
        self.close()
        x, y = self.pointer()
        win = tk.Toplevel(self.root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER)
        try:
            win.wm_attributes("-type", "popup_menu")
        except Exception:
            pass

        frame = tk.Frame(win, bg=BG, padx=14, pady=10)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        tk.Label(frame, text=source, bg=BG, fg=SUB, font=(FONT_UI, 9),
                 justify="left", wraplength=420, anchor="w").pack(fill="x")
        tk.Label(frame, text=result, bg=BG, fg=FG, font=(FONT_UI, 11),
                 justify="left", wraplength=420, anchor="w").pack(fill="x", pady=(8, 0))

        if not pending:
            bar = tk.Frame(frame, bg=BG)
            bar.pack(fill="x", pady=(8, 0))
            copy = tk.Label(bar, text="复制译文", bg=BG, fg="#2563EB",
                            font=(FONT_UI, 8), cursor="hand2")
            copy.pack(side="left")
            copy.bind("<Button-1>", lambda e: self._copy(result))
            tk.Label(bar, text="Esc / 点击关闭", bg=BG, fg="#9CA3AF",
                     font=(FONT_UI, 8)).pack(side="right")

        win.update_idletasks()
        ww, wh = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("+%d+%d" % (min(max(0, x + 14), sw - ww - 8),
                                 min(max(0, y + 20), sh - wh - 8)))

        win.bind("<Escape>", lambda e: self.close())
        win.bind("<Button-1>", lambda e: self.close())
        if not pending:
            self.timer = self.root.after(timeout_ms, self.close)

    def _copy(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as exc:
            log_error("复制失败：%s" % exc)


# ---------------------------------------------------------------- 主程序


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = queue.Queue()
        self.enabled = True
        self.busy = threading.Lock()
        self.backend = detect_backend()

        self.root = tk.Tk()
        install_crash_logging(self.root)
        try:
            ttk.Style().theme_use("clam")
        except Exception:
            pass
        self.root.title(APP_NAME)
        self.root.geometry("600x500")
        self.root.minsize(560, 460)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.popup = Popup(self.root)
        self._build_ui()
        if self.backend:
            PrimaryWatcher(self.events, self, self.backend).start()
        self.root.after(100, self._poll)

    # -- UI ------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        top = tk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="运行中")
        self.status_label = tk.Label(top, textvariable=self.status_var, fg="#059669",
                                     font=(FONT_UI, 12, "bold"))
        self.status_label.pack(side="left")
        self.toggle_btn = tk.Button(top, text="暂停划词", width=10, command=self.toggle)
        self.toggle_btn.pack(side="right")

        hint = "读取方式：%s" % (self.backend or "未找到 xclip/xsel/wl-paste")
        tk.Label(top, text=hint, fg="#059669" if self.backend else "#DC2626",
                 font=(FONT_UI, 9)).pack(side="right", padx=10)

        if not self.backend:
            warn = tk.Label(self.root, fg="#DC2626", font=(FONT_UI, 9), justify="left",
                            text="缺少选区读取工具，请先安装：\n"
                                 "  sudo apt install xclip          # X11\n"
                                 "  sudo apt install wl-clipboard   # Wayland")
            warn.pack(fill="x", padx=14, pady=(0, 6))

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
        self.watch_var = tk.BooleanVar(value=self.cfg.get("watch_selection", True))
        tk.Checkbutton(r2, text="自动监听选区（选中即翻译）",
                       variable=self.watch_var).pack(side="left")

        r3 = tk.Frame(box)
        r3.pack(fill="x", padx=10, pady=4)
        tk.Label(r3, text="API Key（免费通道留空）").pack(side="left")
        self.key_entry = tk.Entry(r3, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.key_entry.insert(
            0, self.cfg.get("deepl_api_key") or self.cfg.get("openai_api_key") or "")

        r4 = tk.Frame(box)
        r4.pack(fill="x", padx=10, pady=(4, 10))
        self.autostart_var = tk.BooleanVar(value=self.cfg.get("autostart", False))
        tk.Checkbutton(r4, text="开机自启", variable=self.autostart_var).pack(side="left")
        tk.Button(r4, text="保存设置", command=self.save).pack(side="right")

        logbox = ttk.LabelFrame(self.root, text="运行日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, height=8, state="disabled", wrap="word",
                           font=(FONT_UI, 9), bg="#FAFAFA", relief="flat")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(self.root,
                 text="提示：选中文字后会自动翻译；也可以把 `%s --once` 绑定到桌面快捷键。"
                      % os.path.basename(sys.argv[0]),
                 fg="#9CA3AF", font=(FONT_UI, 8)).pack(pady=(0, 8))

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
        cfg["watch_selection"] = bool(self.watch_var.get())
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
        autostart_dir = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "autostart")
        path = os.path.join(autostart_dir, "screen-translate.desktop")
        if enable:
            os.makedirs(autostart_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\n"
                        "Type=Application\n"
                        "Name=%s\n"
                        "Comment=选中文字即时翻译\n"
                        "Exec=%s\n"
                        "Terminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n" % (APP_NAME, app_command()))
        elif os.path.exists(path):
            os.remove(path)

    # -- 翻译流程 --------------------------------------------------------

    def _work(self, text, coords=None):
        if not self.busy.acquire(blocking=False):
            return
        try:
            text = text.strip()
            if not text:
                return
            if len(text) > self.cfg.get("max_text_len", 800):
                text = text[: self.cfg["max_text_len"]] + "…"
            self.events.put(("pending", text))
            try:
                result = translate(text, self.cfg)
            except urllib.error.HTTPError as exc:
                result = "翻译失败：HTTP %s" % exc.code
            except Exception as exc:
                result = "翻译失败：%s" % friendly_error(exc)
            self.events.put(("result", text, result))
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
                        threading.Thread(target=self._work, args=(item[1],),
                                         daemon=True).start()
                elif kind == "toggle":
                    self.toggle()
                elif kind == "log":
                    self.log_line(item[1])
                elif kind == "pending":
                    self.popup.show(item[1], "翻译中…", pending=True)
                elif kind == "result":
                    self.popup.show(item[1], item[2],
                                    timeout_ms=int(self.cfg.get("popup_timeout_sec", 20)) * 1000)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def quit(self):
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        if not self.backend:
            self.log_line("未检测到 xclip/xsel/wl-paste，自动划词不可用。")
        self.log_line("已启动。在任意程序里选中文字即可翻译。")
        self.root.mainloop()


# ---------------------------------------------------------------- 入口


def acquire_lock():
    """单实例：用 flock 锁住 /tmp 下的锁文件。"""
    if fcntl is None:
        return True
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def run_once(cfg, print_only=False):
    backend = detect_backend()
    if not backend:
        print("缺少选区读取工具，请安装 xclip 或 wl-clipboard", file=sys.stderr)
        return 2
    text = (read_primary(backend) or "").strip()
    if not text:
        print("当前没有选中的文字", file=sys.stderr)
        return 1
    try:
        result = translate(text, cfg)
    except Exception as exc:
        print("翻译失败：%s" % friendly_error(exc), file=sys.stderr)
        return 3
    print(result)
    if print_only or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return 0

    root = tk.Tk()
    install_crash_logging(root)
    root.withdraw()
    popup = Popup(root)
    popup.show(text, result, timeout_ms=int(cfg.get("popup_timeout_sec", 20)) * 1000)
    root.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="划词翻译（Linux）")
    parser.add_argument("--once", action="store_true",
                        help="读取当前选区并弹出翻译窗口（适合绑定桌面快捷键）")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="读取当前选区并只打印译文")
    parser.add_argument("--config", action="store_true", help="打印配置文件路径")
    args = parser.parse_args()

    install_crash_logging()
    cfg = load_config()

    if args.config:
        ensure_config_dir()
        if not os.path.exists(CONFIG_PATH):
            save_config(cfg)
        print(CONFIG_PATH)
        return 0
    if args.once or args.print_only:
        return run_once(cfg, print_only=args.print_only)

    if not acquire_lock():
        print("程序已在运行", file=sys.stderr)
        return 1

    ensure_config_dir()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    App(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
