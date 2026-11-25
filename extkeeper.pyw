# -*- coding: utf-8 -*-
"""
扩展管家 ExtKeeper —— Chrome 多配置文件扩展同步与批量管理助手
商店扩展走 HKCU 组策略同步，本地扩展走解包与定向装载，支持开机自启、默认后台托盘驻留与自动同步。

通道 A｜商店扩展
    写 HKCU\\Software\\Policies\\Google\\Chrome\\ExtensionInstallForcelist，
    不碰 Secure Preferences 的 HMAC 签名（那个每次 Chrome 升级都可能变，
    改坏了整个配置文件的扩展列表会被 Chrome 判废）。

通道 B｜非商店扩展
    两条路都堵死了，实测查证：
      · --load-extension 在 Chrome 137 被移除，142 连 DisableLoadExtensionCommandLineSwitch
        一起删了，158 上彻底无解；
      · 官方策略写明：非域 Windows 上，商店外的扩展不能强装
        （除非加入 AD / Azure AD / 注册 Chrome Enterprise Core）。
    所以这里只能做成「半自动」：
      1) 把 crx 解包到 packs\\<id>\\，同时留一份原 crx 在旁边；
      2) 写 ExtensionInstallSources 放开 file:// 来源（否则 Chrome 拒绝装商店外的包）；
      3) 点一下，只给「还缺这个扩展」的配置各开一个扩展页窗口，
         你在每个窗口里把 packs\\<id>.crx 拖进去就行。

后台与自启
    · 开机自启：写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\ExtKeeper，
      无需管理员权限，通过 pythonw.exe 静默拉起。
    · 默认后台：启动时隐藏到系统托盘，点击主窗关闭按钮缩进托盘而非退出；
      单实例互斥，重复打开会自动唤出已在后台运行的窗口。
    · 后台自动同步（可选）：按选定的基准配置定期检查新增商店扩展并增量同步到策略。
"""

import os
import io
import sys
import json
import time
import base64
import struct
import ctypes
import hashlib
import zipfile
import shutil
import threading
import subprocess
import urllib.parse
import urllib.request
import traceback
import winreg
import ctypes.wintypes as wt
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False
    DND_FILES = "DND_Files"
    TkinterDnD = None

APP_TITLE = "扩展管家 ExtKeeper — Chrome 多配置扩展同步助手"
APP_SHORT_TITLE = "扩展管家 ExtKeeper"
APP_RUN_NAME = "ExtKeeper"
MUTEX_NAME = "Local\\ExtKeeper_SingleInstance_Mutex"
TRAY_WND_CLASS = "ExtKeeperTrayHostClass"
TRAY_WND_TITLE = "ExtKeeperTrayHostWindow"
SHOW_MSG_NAME = "ExtKeeper_ShowMainWindow_Msg"

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.join(HERE, "packs")
POOL_FILE = os.path.join(HERE, "local.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
ICON_FILE = os.path.join(HERE, "extkeeper.ico")

CHROME_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\User Data")
POLICY = r"Software\Policies\Google\Chrome"
FORCELIST = POLICY + r"\ExtensionInstallForcelist"
EXTSET = POLICY + r"\ExtensionSettings"
INSTALL_SOURCES = POLICY + r"\ExtensionInstallSources"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STORE_URL = "https://clients2.google.com/service/update2/crx"
IDCHARS = "abcdefghijklmnop"
HIDDEN_LOC = {5, 6, 10}

# 后台巡检间隔（毫秒）
STATUS_POLL_MS = 15000
AUTO_SYNC_POLL_MS = 60000

DEFAULT_SETTINGS = {
    "start_in_background": False,
    "auto_sync": False,
    "sync_source": "",
}


# ============================================================ 配置持久化与开机自启

def load_settings():
    """读取本地 settings.json，缺失键用默认值补全"""
    cfg = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_SETTINGS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_settings(cfg):
    """持久化应用偏好设置"""
    os.makedirs(HERE, exist_ok=True)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(cfg or {})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)


def get_pythonw_path():
    """定位用于无黑框启动的 pythonw.exe"""
    exe = os.path.abspath(sys.executable)
    base = os.path.basename(exe).lower()
    if base == "pythonw.exe" and os.path.exists(exe):
        return exe
    if base == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    cand_local = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        r"Programs\Python\Python313\pythonw.exe",
    )
    if os.path.exists(cand_local):
        return cand_local
    found = shutil.which("pythonw.exe")
    return found or exe


def build_autostart_cmd():
    """构造写入注册表 Run 键的开机自启命令"""
    script = os.path.abspath(__file__)
    if getattr(sys, "frozen", False):
        return '"%s" --autostart' % os.path.abspath(sys.executable)
    return '"%s" "%s" --autostart' % (get_pythonw_path(), script)


def is_autostart_enabled():
    """检查当前脚本是否已注册开机自启"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, APP_RUN_NAME)
    except OSError:
        return False
    val_norm = os.path.normcase(str(val))
    target = os.path.normcase(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        target = os.path.normcase(os.path.abspath(sys.executable))
    return target in val_norm


def set_autostart(enabled):
    """开启或关闭开机自启（写 HKCU Run 键，无需管理员权限）"""
    if enabled:
        cmd = build_autostart_cmd()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, APP_RUN_NAME, 0, winreg.REG_SZ, cmd)
        return cmd
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, APP_RUN_NAME)
    except FileNotFoundError:
        pass
    return ""


# ============================================================ 应用图标生成

def ensure_icon_file():
    """确保程序图标 extkeeper.ico 存在（若可用 Pillow 则自动绘制精美多尺寸图标）"""
    if os.path.exists(ICON_FILE):
        return ICON_FILE
    try:
        from PIL import Image, ImageDraw
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # 圆角蓝色底板
        pad = 14
        d.rounded_rectangle(
            (pad, pad, size - pad, size - pad),
            radius=54,
            fill=(37, 99, 235, 255),
        )
        # 顶部微妙高光边
        d.rounded_rectangle(
            (pad + 6, pad + 6, size - pad - 6, size - pad - 6),
            radius=48,
            outline=(96, 165, 250, 140),
            width=4,
        )
        # 中央抽象扩展拼图块（白底 + 右/上凸耳，简洁醒目，缩至 16x16 依然清晰）
        bx0, by0, bx1, by1 = 68, 86, 174, 192
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=20, fill=(255, 255, 255, 255))
        # 上凸耳
        d.ellipse((100, 56, 142, 98), fill=(255, 255, 255, 255))
        # 右凸耳
        d.ellipse((158, 118, 200, 160), fill=(255, 255, 255, 255))
        # 左凹槽（用主色挖空）
        d.ellipse((50, 118, 90, 158), fill=(37, 99, 235, 255))
        # 右下角同步亮点（青绿色小圆点）
        d.ellipse((166, 166, 214, 214), fill=(16, 185, 129, 255),
                  outline=(255, 255, 255, 255), width=6)

        os.makedirs(HERE, exist_ok=True)
        img.save(ICON_FILE, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
        return ICON_FILE
    except Exception:
        return ""


# ============================================================ 原生 Win32 单实例与系统托盘

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# Win32 常量
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_CHECKED = 0x00000008
MF_SEPARATOR = 0x00000800

TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512
ERROR_ALREADY_EXISTS = 183


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", POINT),
    ]


# 显式声明 64 位安全的 Win32 API 签名
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wt.DWORD
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL

user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
user32.UnregisterClassW.restype = wt.BOOL
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
user32.SetMenuDefaultItem.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wt.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wt.HICON
user32.DestroyIcon.argtypes = [wt.HICON]
user32.DestroyIcon.restype = wt.BOOL

shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL

WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")
WM_SHOW_EXISTING = user32.RegisterWindowMessageW(SHOW_MSG_NAME)


def acquire_single_instance(wake_existing=True):
    """获取单实例互斥锁；若已有实例运行，按需唤醒其主窗并返回 None"""
    hmutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not hmutex:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if wake_existing:
            hwnd = user32.FindWindowW(TRAY_WND_CLASS, TRAY_WND_TITLE)
            if hwnd:
                user32.PostMessageW(hwnd, WM_SHOW_EXISTING, 0, 0)
        kernel32.CloseHandle(hmutex)
        return None
    return hmutex


class NativeTray:
    """零第三方依赖的 Windows 原生系统托盘"""

    MENU_SHOW = 1001
    MENU_REFRESH = 1002
    MENU_SYNC_NOW = 1003
    MENU_AUTOSTART = 1004
    MENU_BG_MODE = 1005
    MENU_AUTO_SYNC = 1006
    MENU_QUIT = 1099

    def __init__(self, app, icon_path=""):
        self.app = app
        self.icon_path = icon_path
        self.hwnd = None
        self.hicon = None
        self._own_icon = False
        self._tooltip = APP_TITLE
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._wndproc = WNDPROC(self._proc)
        self._thread = threading.Thread(target=self._run, name="ExtKeeperTray", daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _load_icon(self):
        if self.icon_path and os.path.exists(self.icon_path):
            h = user32.LoadImageW(
                None, self.icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
            )
            if h:
                self._own_icon = True
                return h
        self._own_icon = False
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _make_nid(self, flags=0):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self.hicon
        nid.szTip = (self._tooltip or APP_TITLE)[:127]
        return nid

    def _add_icon(self):
        if not self.hwnd:
            return
        nid = self._make_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _del_icon(self):
        if not self.hwnd:
            return
        nid = self._make_nid(0)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def set_tooltip(self, text):
        self._tooltip = (text or APP_TITLE)[:127]
        if not self.hwnd or self._stopped.is_set():
            return
        try:
            nid = self._make_nid(NIF_TIP)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def notify(self, title, message):
        """发送 Windows 系统托盘气泡通知"""
        if not self.hwnd or self._stopped.is_set():
            return
        try:
            nid = self._make_nid(NIF_INFO)
            nid.szInfoTitle = (title or APP_TITLE)[:63]
            nid.szInfo = (message or "")[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def _show_menu(self):
        state = self.app.get_tray_state()
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            show_label = "隐藏主界面" if state["visible"] else "打开主界面"
            user32.AppendMenuW(menu, MF_STRING, self.MENU_SHOW, show_label)
            user32.SetMenuDefaultItem(menu, self.MENU_SHOW, 0)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_REFRESH, "立即刷新状态")
            sync_flag = MF_STRING if state["has_source"] else (MF_STRING | MF_GRAYED)
            user32.AppendMenuW(
                menu, sync_flag, self.MENU_SYNC_NOW,
                "立即同步（%s）" % state["source_label"] if state["has_source"] else "立即同步（未选基准）"
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if state["autostart"] else 0),
                self.MENU_AUTOSTART,
                "开机自启",
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if state["start_in_bg"] else 0),
                self.MENU_BG_MODE,
                "默认后台运行",
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if state["auto_sync"] else 0),
                self.MENU_AUTO_SYNC,
                "后台自动同步",
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_QUIT, "退出扩展管家")

            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(
                menu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                pt.x, pt.y, 0, self.hwnd, None,
            )
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            if cmd:
                self._dispatch_menu(cmd)
        finally:
            user32.DestroyMenu(menu)

    def _dispatch_menu(self, cmd):
        if cmd == self.MENU_SHOW:
            self.app.call_ui(self.app.toggle_window)
        elif cmd == self.MENU_REFRESH:
            self.app.call_ui(self.app.refresh)
        elif cmd == self.MENU_SYNC_NOW:
            self.app.call_ui(self.app.sync_from_source, True)
        elif cmd == self.MENU_AUTOSTART:
            self.app.call_ui(self.app.toggle_autostart_from_tray)
        elif cmd == self.MENU_BG_MODE:
            self.app.call_ui(self.app.toggle_bg_from_tray)
        elif cmd == self.MENU_AUTO_SYNC:
            self.app.call_ui(self.app.toggle_auto_sync_from_tray)
        elif cmd == self.MENU_QUIT:
            self.app.call_ui(self.app.quit_app)

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            ev = lparam & 0xFFFF
            if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.app.call_ui(self.app.show_window)
                return 0
            if ev == WM_RBUTTONUP:
                self._show_menu()
                return 0
        elif msg == WM_SHOW_EXISTING and WM_SHOW_EXISTING:
            self.app.call_ui(self.app.show_window)
            return 0
        elif msg == WM_TASKBARCREATED and WM_TASKBARCREATED:
            self._add_icon()
            return 0
        elif msg == WM_CLOSE:
            self._del_icon()
            user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_WND_CLASS
        user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, TRAY_WND_CLASS, TRAY_WND_TITLE, 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not self.hwnd:
            self._ready.set()
            return

        self.hicon = self._load_icon()
        self._add_icon()
        self._ready.set()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._del_icon()
        if self._own_icon and self.hicon:
            user32.DestroyIcon(self.hicon)
        user32.UnregisterClassW(TRAY_WND_CLASS, hinst)
        self.hwnd = None


# ============================================================ CRX / ID

def _varint(buf, i):
    r = 0
    s = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, i
        s += 7


def _pb_fields(buf):
    i = 0
    n = len(buf)
    while i < n:
        try:
            key, i = _varint(buf, i)
        except IndexError:
            return
        field, wire = key >> 3, key & 7
        if wire == 0:
            _, i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            yield field, buf[i:i + ln]
            i += ln
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            return


def id_from_bytes(b16):
    return "".join(IDCHARS[int(c, 16)] for c in b16.hex())


def read_crx(path):
    """-> (id, name, version, zip_payload)"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"Cr24":
        raise ValueError("不是 CRX 文件（缺少 Cr24 头）")
    ver = struct.unpack("<I", data[4:8])[0]

    if ver == 3:
        hlen = struct.unpack("<I", data[8:12])[0]
        header = data[12:12 + hlen]
        payload = data[12 + hlen:]
        crx_id = None
        for f, v in _pb_fields(header):
            if f == 10000:
                for f2, v2 in _pb_fields(v):
                    if f2 == 1 and len(v2) == 16:
                        crx_id = v2
        if not crx_id:
            raise ValueError("CRX3 头里没找到 crx_id")
        eid = id_from_bytes(crx_id)
    elif ver == 2:
        pklen = struct.unpack("<I", data[8:12])[0]
        siglen = struct.unpack("<I", data[12:16])[0]
        pubkey = data[16:16 + pklen]
        payload = data[16 + pklen + siglen:]
        eid = id_from_bytes(hashlib.sha256(pubkey).digest()[:16])
    else:
        raise ValueError("不认识的 CRX 版本：%d" % ver)

    name = version = ""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            man = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
        name = man.get("name", "") or ""
        version = man.get("version", "") or ""
        if name.startswith("__MSG_"):
            name = ""
    except Exception:
        pass
    return eid, name, version, payload


def read_folder(path):
    mf = os.path.join(path, "manifest.json")
    if not os.path.exists(mf):
        raise ValueError("这个文件夹里没有 manifest.json")
    with open(mf, encoding="utf-8-sig") as f:
        man = json.load(f)
    name = man.get("name", "") or ""
    version = man.get("version", "") or ""
    if name.startswith("__MSG_"):
        name = ""
    eid = None
    if man.get("key"):
        eid = id_from_bytes(hashlib.sha256(base64.b64decode(man["key"])).digest()[:16])
    if not eid:
        base = os.path.basename(path.rstrip("\\/"))
        if len(base) == 32 and all(c in IDCHARS for c in base):
            eid = base
    if not eid:
        raise ValueError("读不出扩展 ID（manifest 没有 key，目录名也不是 ID）")
    return eid, name, version


def guess_id(text):
    for tok in (text or "").replace("?", "/").replace("&", "/").replace("=", "/").split("/"):
        if len(tok) == 32 and all(c in IDCHARS for c in tok):
            return tok
    return None


def store_has(eid):
    """查商店里有没有这个扩展：True / False / None(查询失败)"""
    try:
        x = urllib.parse.quote("id=%s&uc" % eid)
        url = ("https://clients2.google.com/service/update2/crx"
               "?response=updatecheck&x=" + x)
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
        if 'status="ok"' in body or 'status="noupdate"' in body:
            return True
        if "error-unknownApplication" in body:
            return False
        return None
    except Exception:
        return None


# ============================================================ 配置 / 扩展

def list_profiles():
    out = []
    info = {}
    try:
        with open(os.path.join(CHROME_DATA, "Local State"), encoding="utf-8") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
    except Exception:
        pass
    if not os.path.isdir(CHROME_DATA):
        return out
    for d in os.listdir(CHROME_DATA):
        p = os.path.join(CHROME_DATA, d)
        if not os.path.isdir(p) or d in ("System Profile", "Guest Profile", "Guest Profile 2"):
            continue
        if not (os.path.exists(os.path.join(p, "Preferences"))
                or os.path.exists(os.path.join(p, "Secure Preferences"))):
            continue
        out.append((d, info.get(d, {}).get("name") or d))
    out.sort(key=lambda x: (x[0] != "Default", x[0]))
    return out


def read_extensions(profile_dir):
    res = {}
    for fn in ("Secure Preferences", "Preferences"):
        p = os.path.join(CHROME_DATA, profile_dir, fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for eid, v in (d.get("extensions", {}).get("settings", {}) or {}).items():
            man = v.get("manifest", {}) or {}
            nm = man.get("name", "") or ""
            if nm.startswith("__MSG_"):
                nm = ""
            if eid in res and res[eid]["raw_name"]:
                continue
            res[eid] = {
                "name": nm or eid[:8],
                "raw_name": nm,
                "version": man.get("version", "") or "",
                "location": v.get("location"),
                "path": v.get("path", "") or "",
            }
    return res


# ============================================================ 组策略

def _enum_values(key):
    out = []
    i = 0
    while True:
        try:
            n, v, _ = winreg.EnumValue(key, i)
        except OSError:
            return out
        out.append((n, v))
        i += 1


def read_forcelist():
    out = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, FORCELIST)
    except FileNotFoundError:
        return out
    for n, v in _enum_values(k):
        eid = str(v).split(";")[0].strip()
        if eid:
            out[eid] = str(n)
    winreg.CloseKey(k)
    return out


def write_forcelist(ids):
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, FORCELIST, 0, winreg.KEY_ALL_ACCESS)
    for n, _ in _enum_values(k):
        try:
            winreg.DeleteValue(k, n)
        except OSError:
            pass
    for idx, eid in enumerate(ids, 1):
        winreg.SetValueEx(k, str(idx), 0, winreg.REG_SZ, eid + ";" + STORE_URL)
    winreg.CloseKey(k)


def read_extsettings():
    out = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, EXTSET)
    except FileNotFoundError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        mode = "?"
        try:
            sk = winreg.OpenKey(k, sub)
            try:
                mode = winreg.QueryValueEx(sk, "installation_mode")[0]
            except OSError:
                pass
            winreg.CloseKey(sk)
        except OSError:
            pass
        out[sub] = mode
    winreg.CloseKey(k)
    return out


def set_normal_installed(eid):
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, EXTSET + "\\" + eid, 0,
                           winreg.KEY_ALL_ACCESS)
    winreg.SetValueEx(k, "installation_mode", 0, winreg.REG_SZ, "normal_installed")
    winreg.SetValueEx(k, "update_url", 0, winreg.REG_SZ, STORE_URL)
    winreg.CloseKey(k)


def allow_local_sources():
    """允许从 file:// 装 crx（Chrome 默认禁止非商店来源）"""
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, INSTALL_SOURCES, 0,
                           winreg.KEY_ALL_ACCESS)
    winreg.SetValueEx(k, "1", 0, winreg.REG_SZ, "file:///*")
    winreg.SetValueEx(k, "2", 0, winreg.REG_SZ, "http://localhost/*")
    winreg.CloseKey(k)


def del_tree(sub):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    subs = []
    i = 0
    while True:
        try:
            subs.append(winreg.EnumKey(k, i))
        except OSError:
            break
        i += 1
    winreg.CloseKey(k)
    for s in subs:
        del_tree(sub + "\\" + s)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
    except OSError:
        pass


# ============================================================ 本地扩展池

def load_pool():
    try:
        with open(POOL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pool(pool):
    os.makedirs(HERE, exist_ok=True)
    with open(POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def unpack_to_packs(eid, payload):
    os.makedirs(PACK_DIR, exist_ok=True)
    out = os.path.join(PACK_DIR, eid)
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(out)
    return out


def find_chrome():
    cands = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return ""


def open_extension_pages(profiles):
    """给「还缺这个扩展」的配置各开一个扩展页窗口，返回开了几个"""
    chrome = find_chrome()
    if not chrome:
        return 0
    n = 0
    for pdir, _ in profiles:
        try:
            subprocess.Popen([chrome, "--profile-directory=" + pdir,
                              "--new-window", "chrome://extensions"],
                             close_fds=True)
            n += 1
            time.sleep(0.8)
        except Exception:
            pass
    return n


# ============================================================ 杂项

def chrome_running():
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p

    class PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snap = k32.CreateToolhelp32Snapshot(2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return False
    pe = PE32()
    pe.dwSize = ctypes.sizeof(PE32)
    ok = k32.Process32First(ctypes.c_void_p(snap), ctypes.byref(pe))
    found = False
    while ok:
        if pe.szExeFile.lower() == b"chrome.exe":
            found = True
            break
        ok = k32.Process32Next(ctypes.c_void_p(snap), ctypes.byref(pe))
    k32.CloseHandle(ctypes.c_void_p(snap))
    return found


def chrome_version():
    try:
        with open(os.path.join(CHROME_DATA, "Last Version"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "?"


def profile_header_label(pdir, pname, dup_names):
    """为配置列生成清晰简短的表头；若配置名重复则附带目录编号区分"""
    if pname in dup_names:
        suffix = pdir.replace("Profile ", "").strip()
        if suffix and suffix != pname:
            return "%s%s" % (pname, suffix)
    return pname


def _fit_text(text, max_units):
    """按视觉宽度截断文本（中文计 2，ASCII 计 1）"""
    used = 0
    out = []
    for ch in text:
        u = 2 if ord(ch) > 127 else 1
        if used + u > max_units:
            # 为省略号留出空间
            while out and used + 1 > max_units:
                last = out.pop()
                used -= 2 if ord(last) > 127 else 1
            return "".join(out) + "…"
        out.append(ch)
        used += u
    return text


# ============================================================ 界面

C_BG = "#F5F6F8"
C_CARD = "#FFFFFF"
C_BORDER = "#E4E7EC"
C_TEXT = "#1F2328"
C_MUTED = "#6B7280"
C_FAINT = "#9CA3AF"
C_ACCENT = "#2563EB"
C_ACCENT2 = "#1D4ED8"
C_GREEN = "#0B7A5A"
C_AMBER = "#B45309"
C_RED = "#C0392B"

F_UI = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_H1 = ("Microsoft YaHei UI", 16, "bold")
F_BOLD = ("Microsoft YaHei UI", 10, "bold")
F_MONO = ("Consolas", 9)


class App:
    def __init__(self, root, start_hidden=False, launched_by_autostart=False, icon_path=""):
        self.root = root
        self.icon_path = icon_path
        self.settings = load_settings()
        self.profiles = []
        self.ext_map = {}
        self.force = {}
        self.extset = {}
        self.pool = load_pool()
        self.pending = None
        self.row_ids = {}
        self._hovering = False
        self._hidden = bool(start_hidden)
        self._notified_tray_hide = False
        self._quitting = False
        self._query_seq = 0
        self._last_sync_sig = None

        self.show_builtin = tk.BooleanVar(value=False)
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.start_in_bg_var = tk.BooleanVar(value=bool(self.settings.get("start_in_background", False)))
        self.auto_sync_var = tk.BooleanVar(value=bool(self.settings.get("auto_sync", False)))

        root.title(APP_TITLE)
        root.geometry("1180x900")
        root.minsize(1080, 880)
        root.configure(bg=C_BG)
        if icon_path and os.path.exists(icon_path):
            try:
                root.iconbitmap(icon_path)
            except Exception:
                pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._style()
        self._build()

        self.tray = NativeTray(self, icon_path=icon_path)
        self.tray.start()

        self.refresh()

        if self._hidden:
            # 启动前保持隐藏，不闪主窗
            self.root.withdraw()
            if not launched_by_autostart:
                self.tray.notify(
                    APP_TITLE,
                    "已在后台托盘启动，点击右下角图标可打开主界面。",
                )
        else:
            self.root.deiconify()

        self._schedule_polls()

    # -------------------------------------------------- 线程安全调度与托盘状态

    def call_ui(self, fn, *args, **kwargs):
        """从后台线程安全投递回调至 Tkinter 主线程"""
        if self._quitting:
            return
        try:
            self.root.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def get_tray_state(self):
        """供托盘右键菜单读取当前状态快照"""
        src_dir, src_name = self._current_source_profile()
        return {
            "visible": not self._hidden,
            "autostart": bool(self.autostart_var.get()),
            "start_in_bg": bool(self.start_in_bg_var.get()),
            "auto_sync": bool(self.auto_sync_var.get()),
            "has_source": bool(src_dir),
            "source_label": src_name or src_dir,
        }

    def _current_source_profile(self):
        idx = self.src_box.current()
        if 0 <= idx < len(self.profiles):
            return self.profiles[idx]
        saved = self.settings.get("sync_source", "")
        for d, n in self.profiles:
            if d == saved:
                return d, n
        return ("", "")

    def _persist_settings(self):
        src_dir, _ = self._current_source_profile()
        self.settings["start_in_background"] = bool(self.start_in_bg_var.get())
        self.settings["auto_sync"] = bool(self.auto_sync_var.get())
        self.settings["sync_source"] = src_dir
        try:
            save_settings(self.settings)
        except Exception as e:
            self.log("保存设置失败：%s" % e, "warn")

    # -------------------------------------------------- 窗口显示 / 隐藏 / 退出

    def show_window(self):
        self._hidden = False
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(120, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:
            pass
        self.refresh()

    def hide_to_tray(self, notify_first=True):
        self._hidden = True
        try:
            self.root.withdraw()
        except Exception:
            pass
        if notify_first and not self._notified_tray_hide:
            self._notified_tray_hide = True
            self.tray.notify(
                APP_TITLE,
                "已收起到后台托盘运行，点击图标可随时打开，右键可退出。",
            )

    def toggle_window(self):
        if self._hidden:
            self.show_window()
        else:
            self.hide_to_tray(notify_first=False)

    def on_close(self):
        if self.start_in_bg_var.get():
            self.hide_to_tray(notify_first=True)
        else:
            self.quit_app()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        self._persist_settings()
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    # -------------------------------------------------- 开机自启 / 默认后台 / 自动同步开关

    def on_toggle_autostart(self):
        want = bool(self.autostart_var.get())
        try:
            cmd = set_autostart(want)
            actual = is_autostart_enabled()
            self.autostart_var.set(actual)
            if actual:
                self.log("已开启开机自启（HKCU Run：%s）" % cmd)
            else:
                self.log("已关闭开机自启。")
        except Exception as e:
            self.autostart_var.set(is_autostart_enabled())
            self.log("设置开机自启失败：%s" % e, "warn")
            messagebox.showwarning("开机自启设置失败", str(e))

    def on_toggle_bg_mode(self):
        self._persist_settings()
        if self.start_in_bg_var.get():
            self.log("已开启默认后台：下次启动直接进系统托盘，点关闭按钮也仅隐藏到托盘。")
        else:
            self.log("已关闭默认后台：下次启动直接显示主界面，点关闭按钮将退出程序。")

    def on_toggle_auto_sync(self):
        self._persist_settings()
        src_dir, src_name = self._current_source_profile()
        if self.auto_sync_var.get():
            if not src_dir:
                self.auto_sync_var.set(False)
                self._persist_settings()
                messagebox.showinfo("未选择基准配置", "当前未检测到可用 Chrome 配置。")
                return
            self.log("已开启后台自动同步：每分钟检查「%s」新增商店扩展并自动补齐策略。" % src_name)
            self.sync_from_source(interactive=False)
        else:
            self.log("已关闭后台自动同步。")

    def toggle_autostart_from_tray(self):
        self.autostart_var.set(not self.autostart_var.get())
        self.on_toggle_autostart()

    def toggle_bg_from_tray(self):
        self.start_in_bg_var.set(not self.start_in_bg_var.get())
        self.on_toggle_bg_mode()

    def toggle_auto_sync_from_tray(self):
        self.auto_sync_var.set(not self.auto_sync_var.get())
        self.on_toggle_auto_sync()

    def on_source_changed(self, event=None):
        self._persist_settings()
        src_dir, src_name = self._current_source_profile()
        if src_dir:
            self.log("基准配置已设为：%s（%s）" % (src_name, src_dir))

    def _schedule_polls(self):
        if self._quitting:
            return
        self.root.after(STATUS_POLL_MS, self._poll_status)
        self.root.after(AUTO_SYNC_POLL_MS, self._poll_auto_sync)

    def _poll_status(self):
        if self._quitting:
            return
        try:
            # 仅在主窗口可见时完整重绘；隐藏时只轻刷托盘提示
            if not self._hidden:
                self.render_status()
            else:
                self._update_tray_tooltip()
        except Exception:
            pass
        self.root.after(STATUS_POLL_MS, self._poll_status)

    def _poll_auto_sync(self):
        if self._quitting:
            return
        try:
            if self.auto_sync_var.get():
                self.sync_from_source(interactive=False)
        except Exception:
            pass
        self.root.after(AUTO_SYNC_POLL_MS, self._poll_auto_sync)

    def sync_from_source(self, interactive=False):
        """按基准配置增量同步商店扩展到强制策略（只增不删，安全不越权）"""
        src_dir, src_name = self._current_source_profile()
        if not src_dir:
            if interactive:
                self.tray.notify(APP_TITLE, "未找到可用的基准配置，无法同步。")
            return 0
        exts = read_extensions(src_dir)
        store_ids = [e for e, i in exts.items() if i.get("location") == 1]
        cur_force = read_forcelist()
        cur_extset = read_extsettings()
        missing = [e for e in store_ids if e not in cur_force and e not in cur_extset]
        if not missing:
            if interactive:
                self.log("检查完成：「%s」的商店扩展已全部在策略中。" % src_name)
                if self._hidden:
                    self.tray.notify(APP_TITLE, "「%s」的扩展已全部同步，无需变更。" % src_name)
            return 0

        new_ids = list(cur_force.keys()) + missing
        write_forcelist(new_ids)
        names = [exts[e]["name"] for e in missing[:3]]
        summary = "、".join(names) + (" 等" if len(missing) > 3 else "")
        self.log("自动同步：从「%s」补齐 %d 个商店扩展（%s）。" % (src_name, len(missing), summary))
        self.refresh()
        if self._hidden or interactive:
            tip = "已同步 %d 个新扩展（%s）" % (len(missing), summary)
            if chrome_running():
                tip += "，重启 Chrome 生效。"
            self.tray.notify(APP_TITLE, tip)
        return len(missing)

    # -------------------------------------------------- 样式与布局

    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C_BG, foreground=C_TEXT, font=F_UI)

        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("H1.TLabel", background=C_BG, foreground=C_TEXT, font=F_H1)
        st.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED, font=F_SMALL)

        st.configure("TButton", background="#F7F8FA", foreground=C_TEXT,
                     bordercolor=C_BORDER, lightcolor="#F7F8FA",
                     darkcolor="#F7F8FA", focuscolor="#F7F8FA",
                     relief="flat", borderwidth=1, padding=(10, 5), font=F_UI)
        st.map("TButton",
               background=[("active", "#EFF3F9"), ("pressed", "#E6ECF5")],
               bordercolor=[("active", "#C9D5E4")])

        st.configure("Accent.TButton", background=C_ACCENT, foreground="#FFFFFF",
                     bordercolor=C_ACCENT, lightcolor=C_ACCENT, darkcolor=C_ACCENT,
                     focuscolor=C_ACCENT, padding=(10, 6), font=F_BOLD)
        st.map("Accent.TButton",
               background=[("active", C_ACCENT2), ("pressed", C_ACCENT2)],
               bordercolor=[("active", C_ACCENT2)])

        st.configure("Danger.TButton", background="#FFF7F6", foreground=C_RED,
                     bordercolor="#EBD3D0", lightcolor="#FFF7F6", darkcolor="#FFF7F6")
        st.map("Danger.TButton", background=[("active", "#FDF3F2")],
               bordercolor=[("active", "#D9AFAF")])

        st.configure("Ghost.TButton", background=C_BG, foreground=C_MUTED,
                     bordercolor=C_BG, lightcolor=C_BG, darkcolor=C_BG,
                     padding=(8, 5), font=F_SMALL)
        st.map("Ghost.TButton", background=[("active", "#E9EDF3")],
               bordercolor=[("active", "#E9EDF3")])

        st.configure("CardGhost.TButton", background=C_CARD, foreground=C_MUTED,
                     bordercolor=C_CARD, lightcolor=C_CARD, darkcolor=C_CARD,
                     padding=(8, 3), font=F_SMALL)
        st.map("CardGhost.TButton", background=[("active", "#EFF3F9")],
               bordercolor=[("active", "#EFF3F9")])

        st.configure("Treeview", background=C_CARD, fieldbackground=C_CARD,
                     foreground=C_TEXT, borderwidth=0, rowheight=30, font=F_UI)
        st.configure("Treeview.Heading", background="#FAFBFC", foreground=C_MUTED,
                     font=("Microsoft YaHei UI", 8, "bold"), relief="flat",
                     borderwidth=0, padding=(6, 7))
        st.map("Treeview.Heading", background=[("active", "#F1F4F8")])
        st.map("Treeview", background=[("selected", "#EAF0FB")],
               foreground=[("selected", C_TEXT)])

        st.configure("TEntry", fieldbackground=C_CARD, bordercolor=C_BORDER,
                     lightcolor=C_BORDER, darkcolor=C_BORDER, padding=(8, 6))
        st.configure("TCombobox", fieldbackground=C_CARD, background=C_CARD,
                     bordercolor=C_BORDER, lightcolor=C_BORDER, darkcolor=C_BORDER,
                     arrowcolor=C_MUTED, padding=(6, 5))
        st.map("TCombobox", fieldbackground=[("readonly", C_CARD)],
               bordercolor=[("active", "#C9D5E4")])
        st.configure("TCheckbutton", background=C_BG, foreground=C_MUTED, font=F_SMALL)
        st.map("TCheckbutton", background=[("active", C_BG)],
               indicatorcolor=[("selected", C_ACCENT), ("!selected", C_BORDER)])
        st.configure("Card.TCheckbutton", background=C_CARD, foreground=C_TEXT, font=F_SMALL)
        st.map("Card.TCheckbutton", background=[("active", C_CARD)],
               indicatorcolor=[("selected", C_ACCENT), ("!selected", C_BORDER)])
        st.configure("Vertical.TScrollbar", background="#DCE1E8", troughcolor=C_CARD,
                     bordercolor=C_CARD, arrowcolor=C_MUTED, relief="flat",
                     arrowsize=12)

    def _build(self):
        outer = tk.Frame(self.root, bg=C_BG)
        outer.pack(fill="both", expand=True, padx=18, pady=16)

        head = tk.Frame(outer, bg=C_BG)
        head.pack(fill="x")
        tk.Label(head, text="扩展管家", bg=C_BG, fg=C_TEXT, font=F_H1).pack(side="left")
        tk.Label(head, text="　Chrome 扩展同步与批量管理助手", bg=C_BG, fg=C_MUTED,
                 font=F_SMALL).pack(side="left", pady=(5, 0))
        self.ver_lb = tk.Label(head, text="", bg=C_BG, fg=C_FAINT,
                               font=("Microsoft YaHei UI", 8))
        self.ver_lb.pack(side="left", padx=(10, 0), pady=(6, 0))
        ttk.Button(head, text="刷新", style="Ghost.TButton",
                   command=self.refresh).pack(side="right")
        self.pill = tk.Label(head, text="", bg="#E8F5EE", fg=C_GREEN, font=F_SMALL,
                             padx=10, pady=4)
        self.pill.pack(side="right", padx=(0, 12), pady=(2, 0))

        bar = tk.Frame(outer, bg=C_BG)
        bar.pack(fill="x", pady=(10, 10))
        tk.Label(bar, text="以", bg=C_BG, fg=C_MUTED, font=F_SMALL).pack(side="left")
        self.src_box = ttk.Combobox(bar, state="readonly", width=20, font=F_SMALL)
        self.src_box.pack(side="left", padx=5)
        self.src_box.bind("<<ComboboxSelected>>", self.on_source_changed)
        tk.Label(bar, text="为准，整套铺到全部", bg=C_BG, fg=C_MUTED,
                 font=F_SMALL).pack(side="left")
        ttk.Button(bar, text="铺开", command=self.do_spread).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="自动同步", variable=self.auto_sync_var,
                        command=self.on_toggle_auto_sync).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(bar, text="显示内置组件", variable=self.show_builtin,
                        command=self.render).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="清空全部策略", style="Danger.TButton",
                   command=self.do_clear).pack(side="right")

        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill="both", expand=True)

        # 右栏必须先 pack：左栏带 expand，后 pack 会把右栏挤出窗口
        rightcard = tk.Frame(body, bg=C_CARD, width=338, highlightbackground=C_BORDER,
                             highlightthickness=1)
        rightcard.pack(side="right", fill="y", padx=(12, 0))
        rightcard.pack_propagate(False)

        leftcard = tk.Frame(body, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
        leftcard.pack(side="left", fill="both", expand=True)

        lhead = tk.Frame(leftcard, bg=C_CARD)
        lhead.pack(fill="x", padx=14, pady=(11, 4))
        tk.Label(lhead, text="扩展一览", bg=C_CARD, fg=C_TEXT, font=F_BOLD).pack(side="left")
        self.count_lb = tk.Label(lhead, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL)
        self.count_lb.pack(side="left", padx=8, pady=(2, 0))
        tk.Label(lhead, text="● 已装　○ 未装　·　单击选中　双击复制 ID", bg=C_CARD, fg=C_FAINT,
                 font=F_SMALL).pack(side="right")

        wrap = tk.Frame(leftcard, bg=C_CARD)
        wrap.pack(fill="both", expand=True, padx=(6, 4), pady=(4, 8))
        self.tree = ttk.Treeview(wrap, show="headings", selectmode="browse")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("odd", background="#FBFCFE")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._copy_id)
        self.tree.bind("<Configure>", lambda e: self._resize_cols())

        self.drop = tk.Canvas(rightcard, height=84, bg=C_CARD, highlightthickness=0, cursor="hand2")
        self.drop.pack(fill="x", padx=14, pady=(14, 8))
        self.drop.bind("<Configure>", lambda e: self._draw_drop())
        self.drop.bind("<Button-1>", self._pick_file)
        self._draw_drop()
        if HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)
            self.drop.dnd_bind("<<DropEnter>>", lambda e: self._set_hover(True))
            self.drop.dnd_bind("<<DropLeave>>", lambda e: self._set_hover(False))

        prow = tk.Frame(rightcard, bg=C_CARD)
        prow.pack(fill="x", padx=14)
        self.paste = ttk.Entry(prow, font=F_SMALL)
        self.paste.pack(side="left", fill="x", expand=True)
        ttk.Button(prow, text="识别", command=self.on_paste).pack(side="left", padx=(6, 0))
        self.paste.bind("<Return>", lambda e: self.on_paste())

        self.picked = tk.Label(rightcard, text="还没选扩展 —— 拖入 / 点击选文件 / 点左侧行",
                               bg=C_CARD, fg=C_FAINT, font=F_SMALL, height=2,
                               justify="left", anchor="w", wraplength=304)
        self.picked.pack(fill="x", padx=14, pady=(8, 4))

        self._divider(rightcard)

        tk.Label(rightcard, text="通道 A　商店扩展", bg=C_CARD, fg=C_GREEN,
                 font=F_BOLD).pack(anchor="w", padx=14)
        ttk.Button(rightcard, text="① 强制装到所有配置", style="Accent.TButton",
                   command=self.do_force).pack(fill="x", padx=14, pady=2)
        ttk.Button(rightcard, text="② 装，但允许我自己停用",
                   command=self.do_normal).pack(fill="x", padx=14, pady=2)
        ttk.Button(rightcard, text="③ 一键删除", style="Danger.TButton",
                   command=self.do_remove).pack(fill="x", padx=14, pady=2)

        self._divider(rightcard)

        tk.Label(rightcard, text="通道 B　本地扩展", bg=C_CARD, fg=C_AMBER,
                 font=F_BOLD).pack(anchor="w", padx=14)

        lrow = tk.Frame(rightcard, bg=C_CARD)
        lrow.pack(fill="x", padx=14)
        self.local_lb = tk.Listbox(lrow, height=2, font=F_SMALL, activestyle="none",
                                   exportselection=False, bg="#FCFDFE", fg=C_TEXT,
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=C_BORDER, borderwidth=0,
                                   selectbackground="#EAF0FB", selectforeground=C_TEXT)
        self.local_lb.pack(side="left", fill="x", expand=True)
        ttk.Button(lrow, text="移除", width=5, command=self.do_local_del).pack(
            side="left", padx=(6, 0), anchor="n")

        ttk.Button(rightcard, text="④ 解包并登记",
                   command=self.do_local_add).pack(fill="x", padx=14, pady=(8, 3))
        ttk.Button(rightcard, text="⑤ 给还缺的配置开扩展页",
                   command=self.do_open_pages).pack(fill="x", padx=14, pady=2)

        # 底部记录卡片 + 后台/开机自启选项栏（利用标题行右侧空间，零额外高度消耗）
        footcard = tk.Frame(outer, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
        footcard.pack(fill="x", pady=(12, 0))
        fh = tk.Frame(footcard, bg=C_CARD)
        fh.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(fh, text="记录", bg=C_CARD, fg=C_MUTED, font=F_SMALL).pack(side="left")
        ttk.Button(
            fh, text="最小化到托盘", style="CardGhost.TButton",
            command=lambda: self.hide_to_tray(notify_first=True),
        ).pack(side="right")
        ttk.Checkbutton(
            fh, text="默认后台", style="Card.TCheckbutton",
            variable=self.start_in_bg_var, command=self.on_toggle_bg_mode,
        ).pack(side="right", padx=(0, 12))
        ttk.Checkbutton(
            fh, text="开机自启", style="Card.TCheckbutton",
            variable=self.autostart_var, command=self.on_toggle_autostart,
        ).pack(side="right", padx=(0, 12))

        self.logbox = tk.Text(footcard, height=2, wrap="word", font=F_MONO,
                              bg=C_CARD, fg="#374151", relief="flat", borderwidth=0,
                              highlightthickness=0, padx=14, pady=0,
                              insertbackground=C_TEXT)
        self.logbox.pack(fill="both", expand=True, pady=(3, 8))
        self.logbox.tag_configure("ts", foreground="#9CA3AF")
        self.logbox.tag_configure("warn", foreground=C_AMBER)

    def _divider(self, parent):
        tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", padx=14, pady=7)

    def _set_hover(self, state):
        self._hovering = state
        self._draw_drop()

    def _draw_drop(self):
        c = self.drop
        c.delete("all")
        w = c.winfo_width()
        if w < 20:
            w = 300
        h = 84
        on = self._hovering
        c.create_rectangle(3, 3, w - 3, h - 3, outline=C_ACCENT if on else "#CBD5E1",
                           width=2, dash=(5, 4), fill="#F5F9FF" if on else C_CARD)
        if HAS_DND:
            t1, t2 = "把 .crx 拖到这里", "或点击选择 · 也支持解包目录"
        else:
            t1, t2 = "点击选择 .crx 扩展包", "安装 tkinterdnd2 可开启拖拽"
        c.create_text(w / 2, 25, text=t1, fill=C_TEXT,
                      font=("Microsoft YaHei UI", 11, "bold"))
        c.create_text(w / 2, 46, text=t2, fill=C_MUTED, font=F_SMALL)
        c.create_text(w / 2, 65, text="自动查商店，判定走 A 还是 B",
                      fill=C_FAINT, font=("Microsoft YaHei UI", 8))

    def _pick_file(self, event=None):
        p = filedialog.askopenfilename(
            title="选择 Chrome 扩展安装包 (.crx)",
            filetypes=[("Chrome 扩展包", "*.crx"), ("所有文件", "*.*")],
        )
        if p:
            self._handle_path(p)

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        eid = self.row_ids.get(sel[0])
        if not eid:
            return
        entry = self.ext_map.get(eid)
        name = entry["name"] if entry else ""
        ver = entry["info"].get("version", "") if entry else ""
        if eid in self.force:
            kind = "策略·强制"
        elif eid in self.extset:
            kind = "策略·可停"
        elif entry and entry["info"].get("location") == 1:
            kind = "商店"
        else:
            kind = "已装"
        self.set_pending(eid, name, ver, "table", kind, log_it=False)

    def _copy_id(self, event=None):
        item = self.tree.focus()
        eid = self.row_ids.get(item)
        if not eid:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(eid)
        self.log("已复制 ID：%s" % eid)

    # -------------------------------------------------- 数据与渲染

    def log(self, msg, kind=None):
        self.logbox.insert("end", time.strftime("%H:%M:%S  "), "ts")
        self.logbox.insert("end", msg + "\n", ("warn",) if kind == "warn" else ())
        self.logbox.see("end")

    def refresh(self):
        self.profiles = list_profiles()
        self.ext_map = {}
        for pdir, _ in self.profiles:
            for eid, info in read_extensions(pdir).items():
                d = self.ext_map.get(eid)
                if d is None:
                    d = self.ext_map[eid] = {"name": info["name"], "info": info, "where": {}}
                elif (not d["name"] or d["name"] == eid[:8]) and info["name"] != eid[:8]:
                    d["name"] = info["name"]
                    d["info"] = info
                d["where"][pdir] = info
        self.force = read_forcelist()
        self.extset = read_extsettings()
        self.autostart_var.set(is_autostart_enabled())

        prev_dir, _ = self._current_source_profile()
        target_dir = prev_dir or self.settings.get("sync_source", "")
        self.src_box["values"] = ["%s（%s）" % (n, d) for d, n in self.profiles]
        if self.profiles:
            pick_idx = 0
            for idx, (d, _) in enumerate(self.profiles):
                if d == target_dir:
                    pick_idx = idx
                    break
            self.src_box.current(pick_idx)

        self.render_local()
        self.render()
        self.render_status()

    def _build_status_parts(self):
        total = len(self.profiles)
        stuck = 0
        for eid in self.force:
            where = self.ext_map.get(eid, {}).get("where", {})
            if sum(1 for d, _ in self.profiles if d in where) < total:
                stuck += 1
        parts = ["%d 配置" % total, "%d 策略" % (len(self.force) + len(self.extset))]
        if stuck:
            parts.append("%d 缺" % stuck)
        if self.pool:
            parts.append("%d 本地" % len(self.pool))
        run = chrome_running()
        parts.append("Chrome " + ("开" if run else "关"))
        return parts, run

    def _update_tray_tooltip(self):
        parts, _ = self._build_status_parts()
        tip = "%s - %s" % (APP_SHORT_TITLE, " · ".join(parts[:3]))
        if self.auto_sync_var.get():
            tip += " · 自动同步开"
        self.tray.set_tooltip(tip)

    def render_status(self):
        parts, run = self._build_status_parts()
        self.pill.configure(text="  ·  ".join(parts),
                            bg="#FDF1E3" if run else "#E8F5EE",
                            fg=C_AMBER if run else C_GREEN)
        self.ver_lb.configure(text=chrome_version())
        self._update_tray_tooltip()

    def render_local(self):
        self.local_lb.delete(0, "end")
        for p in self.pool:
            where = self.ext_map.get(p["id"], {}).get("where", {})
            have = sum(1 for d, _ in self.profiles if d in where)
            self.local_lb.insert("end", "%s  %d/%d" % (
                p.get("name") or p["id"][:8], have, len(self.profiles)))

    def _col_widths(self):
        total = self.tree.winfo_width()
        if total < 200:
            total = 800
        n = max(1, len(self.profiles))
        pol_w, id_w = 58, 118
        name_w = max(185, min(295, int(total * 0.27)))
        pw = max(56, (total - name_w - id_w - pol_w - 6) // n)
        return name_w, id_w, pw, pol_w

    def _dup_profile_names(self):
        counts = {}
        for _, n in self.profiles:
            counts[n] = counts.get(n, 0) + 1
        return {n for n, c in counts.items() if c > 1}

    def _apply_profile_headings(self, pw):
        dup = self._dup_profile_names()
        max_units = max(4, (pw - 12) // 8)
        for i, (d, n) in enumerate(self.profiles):
            label = profile_header_label(d, n, dup)
            shown = _fit_text(label, max_units)
            self.tree.heading("p%d" % i, text=shown)
            self.tree.column("p%d" % i, width=pw, anchor="center")

    def _resize_cols(self):
        if not self.profiles:
            return
        try:
            name_w, id_w, pw, pol_w = self._col_widths()
            self.tree.column("name", width=name_w)
            self.tree.column("id", width=id_w)
            self._apply_profile_headings(pw)
            self.tree.column("pol", width=pol_w)
        except Exception:
            pass

    def render(self):
        profs = [p[0] for p in self.profiles]
        cols = ["name", "id"] + ["p%d" % i for i in range(len(profs))] + ["pol"]
        self.tree.configure(columns=cols)
        name_w, id_w, pw, pol_w = self._col_widths()
        self.tree.heading("name", text="扩展")
        self.tree.column("name", width=name_w, anchor="w")
        self.tree.heading("id", text="ID")
        self.tree.column("id", width=id_w, anchor="w")
        self._apply_profile_headings(pw)
        self.tree.heading("pol", text="策略")
        self.tree.column("pol", width=pol_w, anchor="center")

        for i in self.tree.get_children():
            self.tree.delete(i)
        self.row_ids = {}

        rows = []
        for eid, d in self.ext_map.items():
            loc = d["info"].get("location")
            forced = eid in self.force
            normal = self.extset.get(eid) in ("normal_installed", "force_installed")
            if loc in HIDDEN_LOC and not (forced or normal) and not self.show_builtin.get():
                continue
            cells = ["●" if p in d["where"] else "○" for p in profs]
            tag = "强制" if forced else ("可停" if normal else "")
            short = (eid[:8] + "…") if len(eid) > 8 else eid
            rows.append((d["name"], eid, short, cells, tag))

        for eid in list(self.force.keys()) + [e for e in self.extset if e not in self.force]:
            if eid in self.ext_map:
                continue
            rows.append(("未落地 · " + eid[:8], eid, eid[:8] + "…",
                         ["○"] * len(profs),
                         "强制" if eid in self.force else "可停"))

        rows.sort(key=lambda r: r[0].lower())
        for idx, (name, eid, short, cells, tag) in enumerate(rows):
            iid = self.tree.insert("", "end", values=[name, short] + cells + [tag],
                                   tags=("odd",) if idx % 2 else ())
            self.row_ids[iid] = eid

        self.count_lb.configure(text="共 %d 个" % len(rows))

    # -------------------------------------------------- 选择与异步商店检查

    def set_pending(self, eid, name, version, source, kind, log_it=True):
        self.pending = {"id": eid, "name": name, "version": version,
                        "source": source, "kind": kind}
        where = len(self.ext_map.get(eid, {}).get("where", {}))
        color = {
            "商店": C_GREEN,
            "本地": C_AMBER,
            "策略·强制": C_ACCENT,
            "策略·可停": C_GREEN,
        }.get(kind, C_TEXT)
        line1 = "%s · %s  v%s" % (kind or "未知", name or "（无名）", version or "?")
        line1 = _fit_text(line1, 30)
        short_id = (eid[:14] + "…") if len(eid) > 14 else eid
        self.picked.configure(
            text="%s\n%s  ·  已装 %d / %d"
                 % (line1, short_id, where, len(self.profiles)),
            fg=color)
        if log_it:
            self.log("识别到 %s  %s  [%s]" % (name or "（无名）", eid, kind))

    def _query_store_async(self, eid, name, ver, source, local_hint=False):
        """后台线程查询商店是否存在，避免阻塞界面"""
        self._query_seq += 1
        seq = self._query_seq
        self.set_pending(eid, name, ver, source, "查商店中…", log_it=False)
        self.log("查商店…")

        def worker():
            has = store_has(eid)
            kind = {True: "商店", False: "本地", None: "查询失败"}[has]

            def finish():
                if seq != self._query_seq:
                    return
                self.set_pending(eid, name, ver, source, kind)
                if local_hint and kind == "本地":
                    self.log("商店里没有 —— 走通道 B。")

            self.call_ui(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_path(self, p):
        self.log("拖入：%s" % p)
        try:
            if os.path.isdir(p):
                eid, name, ver = read_folder(p)
                self.set_pending(eid, name, ver, p, "文件夹")
            elif p.lower().endswith(".crx"):
                eid, name, ver, _ = read_crx(p)
                self._query_store_async(eid, name, ver, p, local_hint=True)
            else:
                gid = guess_id(p)
                if not gid:
                    raise ValueError("认不出来：只支持 .crx / 解包目录 / 商店链接 / 32 位 ID")
                self._query_store_async(gid, "", "", p, local_hint=False)
        except Exception as e:
            self.log("失败：%s" % e, "warn")
            messagebox.showwarning("没认出来", str(e))

    def on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data.strip("{}")]
        if not paths:
            return
        self._handle_path(paths[0])

    def on_paste(self):
        gid = guess_id(self.paste.get())
        if not gid:
            messagebox.showwarning("没认出来", "粘贴商店页面链接，或 32 位扩展 ID")
            return
        entry = self.ext_map.get(gid)
        name = entry["name"] if entry else ""
        ver = entry["info"].get("version", "") if entry else ""
        self._query_store_async(gid, name, ver, "text", local_hint=False)

    # -------------------------------------------------- 通道 A

    def _need(self):
        if not self.pending:
            messagebox.showinfo("先选一个", "先把 .crx 拖进来，粘贴链接 / ID，或单击左侧列表中的扩展。")
            return None
        return self.pending

    def _after(self):
        if chrome_running():
            self.log("Chrome 正开着 —— 请完全退出（含托盘）再打开才生效。")
        self.refresh()

    def do_force(self):
        p = self._need()
        if not p:
            return
        if not messagebox.askokcancel(
                "确认", "把 %s\n强制装进全部 %d 个配置？\n\n"
                        "强制装的扩展界面上不能停用、不能卸载，会显示「由贵单位管理」。"
                        % (p["name"] or p["id"], len(self.profiles))):
            return
        ids = list(self.force.keys())
        if p["id"] not in ids:
            ids.append(p["id"])
        write_forcelist(ids)
        if p["id"] in self.extset:
            del_tree(EXTSET + "\\" + p["id"])
        self.log("已强装 %s" % p["id"])
        self._after()

    def do_normal(self):
        p = self._need()
        if not p:
            return
        set_normal_installed(p["id"])
        if p["id"] in self.force:
            write_forcelist([i for i in self.force if i != p["id"]])
        self.log("已设为「自动装、可停用」：%s" % p["id"])
        self._after()

    def do_remove(self):
        p = self._need()
        if not p:
            return
        if p["id"] in self.force:
            write_forcelist([i for i in self.force if i != p["id"]])
        if p["id"] in self.extset:
            del_tree(EXTSET + "\\" + p["id"])
        self.log("已从策略撤掉 %s" % p["id"])
        self.log("重启 Chrome 后，策略装上的会自动卸；原先手动装的可能留下。")
        self._after()

    def do_clear(self):
        if not messagebox.askokcancel("确认",
                                      "清空全部扩展策略？只动「强制安装 / 扩展设置 / 安装来源」三项。"):
            return
        del_tree(FORCELIST)
        del_tree(EXTSET)
        del_tree(INSTALL_SOURCES)
        self.log("策略已清空。")
        self._after()

    def do_spread(self):
        idx = self.src_box.current()
        if idx < 0:
            return
        pdir, pname = self.profiles[idx]
        exts = read_extensions(pdir)
        ids = [e for e, i in exts.items() if i.get("location") == 1]
        if not ids:
            messagebox.showinfo("没有可铺的", "这个配置里没有从商店装的扩展。")
            return
        if not messagebox.askokcancel(
                "确认", "把「%s」里的 %d 个商店扩展强制装进所有配置？\n\n%s"
                        % (pname, len(ids), "\n".join(exts[e]["name"] for e in ids[:10]))):
            return
        cur = list(self.force.keys())
        for e in ids:
            if e not in cur:
                cur.append(e)
        write_forcelist(cur)
        self._persist_settings()
        self.log("已铺开 %d 个扩展。" % len(ids))
        self._after()

    # -------------------------------------------------- 通道 B

    def do_local_add(self):
        p = self._need()
        if not p:
            return
        src = p["source"]
        try:
            os.makedirs(PACK_DIR, exist_ok=True)
            if os.path.isdir(src):
                dst = os.path.join(PACK_DIR, p["id"])
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
            elif os.path.isfile(src) and src.lower().endswith(".crx"):
                eid, name, ver, payload = read_crx(src)
                unpack_to_packs(eid, payload)
                shutil.copy2(src, os.path.join(PACK_DIR, p["id"] + ".crx"))
            else:
                raise ValueError("这一条只能从 .crx 或文件夹来，光有 ID 解不了包")
            self.pool = [x for x in self.pool if x["id"] != p["id"]]
            self.pool.append({"id": p["id"], "name": p["name"] or p["id"][:8],
                              "version": p["version"] or ""})
            save_pool(self.pool)
            self.render_local()
            self.log("已解包到 packs\\%s" % p["id"])
            allow_local_sources()
            self.log("已放开 file:// 安装来源，以后拖 crx 进扩展页不会被拦。")
        except Exception as e:
            self.log("解包失败：%s" % e, "warn")
            messagebox.showwarning("解包失败", str(e))

    def do_allow(self):
        allow_local_sources()
        self.log("已放开 file:// 安装来源（ExtensionInstallSources）。")
        messagebox.showinfo("好了",
                            "Chrome 完全退出再打开，之后把 crx 拖进扩展页就不会被拦。\n"
                            "这一步一次就够，不用每个配置做。")

    def do_open_pages(self):
        sel = self.local_lb.curselection()
        if not sel:
            messagebox.showinfo("先选一个", "在本地扩展列表里选中一个扩展。")
            return
        p = self.pool[sel[0]]
        allow_local_sources()
        where = self.ext_map.get(p["id"], {}).get("where", {})
        missing = [(d, n) for d, n in self.profiles if d not in where]
        if not missing:
            messagebox.showinfo("齐了", "所有配置都已经有这个扩展了。")
            return
        crx = os.path.join(PACK_DIR, p["id"] + ".crx")
        target = crx if os.path.exists(crx) else os.path.join(PACK_DIR, p["id"])
        self.log("还缺 %d 个配置：%s" % (len(missing), "、".join(n for _, n in missing)))
        self.log("拖这个：%s" % target)
        n = open_extension_pages(missing)
        if n == 1:
            messagebox.showinfo("开了 1 个窗口",
                                "把下面这个拖进窗口里的扩展页：\n%s" % target)
        else:
            messagebox.showinfo("开了 %d 个窗口" % n,
                                "每个窗口对应一个还缺它的配置。\n\n"
                                "把下面这个，逐个拖进每个窗口的扩展页：\n%s\n\n"
                                "同一个文件可以拖很多次。" % target)

    def do_local_del(self):
        sel = self.local_lb.curselection()
        if not sel:
            return
        p = self.pool[sel[0]]
        if not messagebox.askokcancel("确认", "从本地池移除 %s？\n（packs 里的文件保留，不会删）"
                                      % (p.get("name") or p["id"])):
            return
        self.pool = [x for x in self.pool if x["id"] != p["id"]]
        save_pool(self.pool)
        self.render_local()
        self.log("已从池里移除 %s" % p["id"])


def main():
    args = {a.lower() for a in sys.argv[1:]}
    force_bg = "--background" in args or "--minimized" in args
    force_show = "--show" in args
    launched_by_autostart = "--autostart" in args

    # 单实例检查：已有实例在后台时，再次打开直接唤醒已有窗口
    hmutex = acquire_single_instance(wake_existing=not force_bg)
    if not hmutex:
        return

    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        cfg = load_settings()
        if force_show:
            start_hidden = False
        elif force_bg:
            start_hidden = True
        else:
            start_hidden = bool(cfg.get("start_in_background", False))

        icon_path = ensure_icon_file()
        root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        if start_hidden:
            root.withdraw()
        app = App(
            root,
            start_hidden=start_hidden,
            launched_by_autostart=launched_by_autostart,
            icon_path=icon_path,
        )
        try:
            root.mainloop()
        finally:
            app.quit_app()
    finally:
        kernel32.CloseHandle(hmutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "error.log"), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        try:
            messagebox.showerror("启动失败", tb[-1500:])
        except Exception:
            pass
