# -*- coding: utf-8 -*-
"""
扩展管家 ExtKeeper —— Chrome 多配置文件扩展同步与批量管理助手
Multi-Profile Chrome Extension Sync & Policy Manager for Windows

通道 A｜商店扩展（Channel A — Chrome Web Store）
    写 HKCU\\Software\\Policies\\Google\\Chrome\\ExtensionInstallForcelist，
    不碰 Secure Preferences 的 HMAC 签名（那个每次 Chrome 升级都可能变，
    改坏了整个配置文件的扩展列表会被 Chrome 判废）。

通道 B｜非商店扩展（Channel B — Local / Private CRX）
    两条路都堵死了，实测查证：
      · --load-extension 在 Chrome 137 被移除，142 连 DisableLoadExtensionCommandLineSwitch
        一起删了，158 上彻底无解；
      · 官方策略写明：非域 Windows 上，商店外的扩展不能强装
        （除非加入 AD / Azure AD / 注册 Chrome Enterprise Core）。
    所以这里做成「半自动」：
      1) 把 crx 解包到 packs\\<id>\\，同时留一份原 crx 在旁边；
      2) 写 ExtensionInstallSources 放开 file:// 来源（否则 Chrome 拒绝装商店外的包）；
      3) 点一下，只给「还缺这个扩展」的配置各开一个扩展页窗口，
         在每个窗口里把 packs\\<id>.crx 拖进去即可。

后台、自启与双语界面
    · 开机自启：写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\ExtKeeper，
      无需管理员权限，通过 pythonw.exe 静默拉起。
    · 默认后台：启动时隐藏到系统托盘，点击主窗关闭按钮缩进托盘而非退出；
      单实例互斥，重复打开会自动唤出已在后台运行的窗口。
    · 后台自动同步（可选）：按选定的基准配置定期检查新增商店扩展并增量同步到策略。
    · 中英文双语界面：支持自动跟随系统语言与一键实时切换（简体中文 / English）。
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
import tempfile
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

APP_RUN_NAME = "ExtKeeper"
MUTEX_NAME = "Local\\ExtKeeper_SingleInstance_Mutex"
TRAY_WND_CLASS = "ExtKeeperTrayHostClass"
TRAY_WND_TITLE = "ExtKeeperTrayHostWindow"
SHOW_MSG_NAME = "ExtKeeper_ShowMainWindow_Msg"

IS_FROZEN = getattr(sys, "frozen", False)
HERE = (
    os.path.dirname(os.path.abspath(sys.executable))
    if IS_FROZEN
    else os.path.dirname(os.path.abspath(__file__))
)
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)
PACK_DIR = os.path.join(HERE, "packs")
POOL_FILE = os.path.join(HERE, "local.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
ICON_FILE = os.path.join(HERE, "extkeeper.ico")
BUNDLED_ICON_FILE = os.path.join(BUNDLE_DIR, "extkeeper.ico")

CHROME_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\User Data")
POLICY = r"Software\Policies\Google\Chrome"
FORCELIST = POLICY + r"\ExtensionInstallForcelist"
EXTSET = POLICY + r"\ExtensionSettings"
INSTALL_SOURCES = POLICY + r"\ExtensionInstallSources"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STORE_URL = "https://clients2.google.com/service/update2/crx"
IDCHARS = "abcdefghijklmnop"
HIDDEN_LOC = {5, 10}
BUILTIN_IDS = {"lmjegmlicamnimmfhcmpkclmigmmcbeh"}  # Google Docs Offline 预装扩展

# 后台巡检间隔（毫秒）
STATUS_POLL_MS = 15000
AUTO_SYNC_POLL_MS = 60000

DEFAULT_SETTINGS = {
    "start_in_background": False,
    "auto_sync": False,
    "sync_source": "",
    "language": "auto",  # "auto" | "zh" | "en"
}


# ============================================================ 国际化（简体中文 / English）

I18N = {
    "zh": {
        "app_title": "扩展管家 ExtKeeper — Chrome 多配置扩展同步助手",
        "app_short": "扩展管家 ExtKeeper",
        "h1": "扩展管家",
        "subtitle": "　Chrome 扩展同步与策略管家",
        "btn_refresh": "刷新",
        "btn_lang_switch": "EN",
        "bar_prefix": "以",
        "bar_suffix": "为准，整套铺到全部",
        "btn_spread": "铺开",
        "chk_auto_sync": "自动同步",
        "chk_builtin": "显示内置组件",
        "btn_kill_chrome": "关闭 Chrome",
        "btn_clear": "清空全部策略",
        "left_title": "扩展一览",
        "left_count": "共 %d 个",
        "left_legend": "● 已装　◐ 待开号生效　○ 未装 · 仅表示本地快照，以打开浏览器为准",
        "col_ext": "扩展",
        "col_id": "ID",
        "col_pol": "策略",
        "tag_force": "强制",
        "tag_normal": "可停",
        "tag_removed": "卸载",
        "tag_blocked": "禁用",
        "unlanded": "未落地 · ",
        "drop_t1_dnd": "把 .crx 拖到这里",
        "drop_t2_dnd": "或点击选择 · 也支持解包目录",
        "drop_t1_click": "点击选择 .crx 扩展包",
        "drop_t2_click": "安装 tkinterdnd2 可开启拖拽",
        "drop_t3": "自动查商店，判定走 A 还是 B",
        "btn_lookup": "识别",
        "picked_empty": "还没选扩展 —— 拖入 / 点击选文件 / 点左侧行",
        "picked_installed": "已装 %d / %d",
        "unnamed": "（无名）",
        "ch_a": "通道 A　商店扩展",
        "btn_force": "① 强制装到所有配置",
        "btn_normal": "② 装，但允许我自己停用",
        "btn_remove": "③ 一键删除",
        "ch_b": "通道 B　本地扩展",
        "btn_local_del": "移除",
        "btn_local_add": "④ 解包并登记",
        "btn_open_pages": "⑤ 给还缺的配置开扩展页",
        "log_title": "记录",
        "btn_elevate": "管理员模式重启",
        "chk_autostart": "开机自启",
        "chk_bg_mode": "默认后台",
        "btn_hide_tray": "最小化到托盘",
        "pill_profiles": "%d 配置",
        "pill_policies": "%d 策略",
        "pill_missing": "%d 缺",
        "pill_local": "%d 本地",
        "pill_chrome_on": "Chrome 开",
        "pill_chrome_off": "Chrome 关",
        "pill_non_admin": "需提权",
        "pill_hklm_conflict": "HKLM冲突",
        "tip_auto_sync_on": "自动同步开",
        "kind_store": "商店",
        "kind_local": "本地",
        "kind_folder": "文件夹",
        "kind_checking": "查商店中…",
        "kind_failed": "查询失败",
        "kind_policy_force": "策略·强制",
        "kind_policy_normal": "策略·可停",
        "kind_policy_removed": "策略·卸载",
        "kind_installed": "已装",
        "kind_unknown": "未知",
        "tray_show": "打开主界面",
        "tray_hide": "隐藏主界面",
        "tray_refresh": "立即刷新状态",
        "tray_kill_chrome": "关闭 Chrome 后台",
        "tray_sync": "立即同步（%s）",
        "tray_sync_none": "立即同步（未选基准）",
        "tray_autostart": "开机自启",
        "tray_bg_mode": "默认后台运行",
        "tray_auto_sync": "后台自动同步",
        "tray_lang_switch": "Switch to English",
        "tray_quit": "退出扩展管家",
        "notify_bg_started": "已在后台托盘启动，点击右下角图标可打开主界面。",
        "notify_bg_hidden": "已收起到后台托盘运行，点击图标可随时打开，右键可退出。",
        "notify_no_source": "未找到可用的基准配置，无法同步。",
        "notify_sync_clean": "「%s」的扩展已全部同步，无需变更。",
        "notify_sync_done": "已同步 %d 个新扩展（%s）",
        "notify_restart_chrome": "，重启 Chrome 并打开对应账户后生效。",
        "log_save_err": "保存设置失败：%s",
        "log_autostart_on": "已开启开机自启（%s）",
        "log_autostart_off": "已关闭开机自启。",
        "log_autostart_err": "设置开机自启失败：%s",
        "log_need_admin": "当前为普通权限：写入组策略时将自动请求 UAC 管理员授权，也可点右下「管理员模式重启」。",
        "log_hklm_conflict": "检测到 HKLM 存在旧扩展策略（会覆盖 HKCU 策略），执行任意安装/删除/清空时将自动清理冲突。",
        "log_uac_cancelled": "已取消管理员授权，本次策略未修改。",
        "log_policy_err": "策略写入失败：%s",
        "dlg_policy_err_title": "策略写入失败",
        "dlg_policy_err_detail": "无法写入 Chrome 组策略：\n%s\n\n请检查以下原因：\n1. 弹出 UAC 管理员授权框时是否点了「是」\n2. 可先点右下角「管理员模式重启」后再试\n3. 杀毒软件 / 安全工具是否拦截了注册表写入",
        "dlg_store_missing_title": "无法通过商店策略安装",
        "dlg_store_missing_msg": "检测到 %s 不在 Chrome 应用商店中。\n\n受 Chrome 平台限制，非域控 Windows 无法通过组策略强制安装商店外扩展。\n请改用下方【通道 B 本地扩展】：\n④ 解包并登记 → ⑤ 给还缺的配置开扩展页，手动拖入即可。",
        "dlg_store_net_title": "商店连通性检测失败",
        "dlg_store_net_msg": "检测扩展 %s 时无法连接 Chrome 应用商店（clients2.google.com）。\n\n请检查网络或代理设置；若无法访问商店，即使写入策略 Chrome 也无法下载安装。\n是否仍要先写入策略？",
        "dlg_apply_ok_title": "策略已写入（需打开账户生效）",
        "dlg_apply_ok_chrome_on": "%s\n\n【如何生效】\n1. 当前 Chrome 仍在运行：请先点界面「关闭 Chrome」彻底退出后台，再重新打开 Chrome；\n2. 依次打开各个 Chrome 账户窗口（未打开的账户不会在后台自动下载/卸载），等待约 5–20 秒；\n3. 新装扩展默认收在右上角 🧩 拼图菜单中，装完后点本软件「刷新」即可更新状态。\n\n【若仍装不上 / 未生效，请检测】\n· 网络/代理能否访问 Chrome 商店（clients2.google.com）\n· 该扩展是否仍在商店正常上架（非商店扩展需走通道 B）\n· 在 Chrome 打开 chrome://policy 确认策略已加载并点「重新加载政策」",
        "dlg_apply_ok_chrome_off": "%s\n\n【如何生效】\n1. 重新打开 Chrome，并依次切到各个账户窗口（未打开的账户不会在后台自动下载/卸载）；\n2. 打开账户后约 5–20 秒自动完成；新装扩展默认收在右上角 🧩 拼图菜单中；\n3. 装完后回到本软件点「刷新」即可查看最新状态。\n\n【若仍装不上 / 未生效，请检测】\n· 网络/代理能否访问 Chrome 商店（clients2.google.com）\n· 该扩展是否仍在商店正常上架（非商店扩展需走通道 B）\n· 在 Chrome 打开 chrome://policy 确认策略已加载并点「重新加载政策」",
        "dlg_need_admin_title": "需要管理员权限",
        "dlg_need_admin_msg": "写入 Chrome 组策略需要管理员权限（拒绝访问）。\n\n是否立即以管理员身份重新启动扩展管家？",
        "log_removed_all": "已写入全局卸载策略：%s（重启 Chrome 后从全部配置卸载）",
        "log_removed_cancel": "已撤销全局卸载策略：%s",
        "dlg_remove_choice_msg": "删除 %s 的方式：\n\n· 点「是」：同时写入全局卸载策略，重启 Chrome 后把所有配置里手动安装的版本也一并卸载（之后仍可重装）\n· 点「否」：仅撤回托管策略（仅卸载策略强装的版本）",
        "dlg_remove_manual_msg": "%s 当前为手动安装（不在托管策略中）。\n\n是否写入 Chrome 全局卸载策略，重启 Chrome 后从全部配置自动卸载它？（卸载后仍可重新从商店安装）",
        "dlg_remove_undo_msg": "%s 当前处于「全局卸载」策略中。\n\n是否撤回该卸载策略（恢复允许正常安装）？",
        "log_bg_on": "已开启默认后台：下次启动直接进系统托盘，点关闭按钮也仅隐藏到托盘。",
        "log_bg_off": "已关闭默认后台：下次启动直接显示主界面，点关闭按钮将退出程序。",
        "log_auto_sync_on": "已开启后台自动同步：每分钟检查「%s」新增商店扩展并自动补齐策略。",
        "log_auto_sync_off": "已关闭后台自动同步。",
        "log_source_set": "基准配置已设为：%s（%s）",
        "log_sync_clean": "检查完成：「%s」的商店扩展已全部在策略中。",
        "log_sync_added": "自动同步：从「%s」补齐 %d 个商店扩展（%s）。",
        "log_lang_changed": "界面语言已切换为：简体中文",
        "log_copied_id": "已复制 ID：%s",
        "log_picked": "识别到 %s  %s  [%s]",
        "log_checking": "查商店…",
        "log_not_in_store": "商店里没有 —— 走通道 B。",
        "log_dropped": "拖入：%s",
        "log_failed": "失败：%s",
        "log_chrome_running": "Chrome 正开着 —— 请点「关闭 Chrome」彻底退出再重开，并打开对应账户窗口才会触发安装/卸载（约 5–20 秒，新装扩展默认收在右上角 🧩 拼图里）。",
        "log_apply_tip": "提示：启动 Chrome 后，需依次打开对应账户窗口才会触发自动安装/卸载（未打开的账户不会在后台下载，约 5–20 秒完成，新装扩展默认收在右上角 🧩 拼图里）。",
        "log_chrome_not_running": "Chrome 当前未在运行，无需关闭。",
        "log_chrome_killed": "已关闭所有 Chrome 窗口与后台进程；重新打开 Chrome 并切到对应账户窗口即可触发策略生效。",
        "log_chrome_kill_left": "已尝试关闭 Chrome，但仍有进程残留（可点右下管理员重启后再试）。",
        "log_chrome_kill_err": "关闭 Chrome 失败：%s",
        "dlg_kill_chrome_msg": "确定关闭所有 Chrome 窗口及后台进程？\n\n会先尝试温和关闭以保留标签页会话，若仍有后台驻留进程再强制结束。",
        "log_forced": "已强装 %s",
        "log_normal": "已设为「自动装、可停用」：%s",
        "log_removed": "已从策略撤掉 %s",
        "log_removed_hint": "重启 Chrome 后，策略装上的会自动卸；原先手动装的可能留下。",
        "log_cleared": "策略已清空。",
        "log_spread": "已铺开 %d 个扩展。",
        "log_unpacked": "已解包到 packs\\%s",
        "log_allowed_src": "已放开 file:// 安装来源，以后拖 crx 进扩展页不会被拦。",
        "log_unpack_err": "解包失败：%s",
        "log_missing_profs": "还缺 %d 个配置：%s",
        "log_drag_target": "拖这个：%s",
        "log_local_removed": "已从池里移除 %s",
        "etc_more": " 等",
        "sep_list": "、",
        "dlg_pick_title": "选择 Chrome 扩展安装包 (.crx)",
        "dlg_crx_filter": "Chrome 扩展包",
        "dlg_all_filter": "所有文件",
        "dlg_autostart_err": "开机自启设置失败",
        "dlg_no_source_title": "未选择基准配置",
        "dlg_no_source_msg": "当前未检测到可用 Chrome 配置。",
        "dlg_unrecognized": "没认出来",
        "dlg_paste_hint": "粘贴商店页面链接，或 32 位扩展 ID",
        "dlg_need_title": "先选一个",
        "dlg_need_msg": "先把 .crx 拖进来，粘贴链接 / ID，或单击左侧列表中的扩展。",
        "dlg_confirm": "确认",
        "dlg_force_msg": "把 %s\n强制装进全部 %d 个配置？\n\n· 强制装的扩展不能手动停用/卸载，会显示「由贵单位管理」\n· 写入后需彻底重启 Chrome，并打开对应账户窗口才会开始下载（约 5–20 秒，默认收在右上角 🧩 拼图里）",
        "dlg_clear_msg": "清空全部扩展策略？只动「强制安装 / 扩展设置 / 安装来源」三项。",
        "dlg_spread_none_title": "没有可铺的",
        "dlg_spread_none_msg": "这个配置里没有从商店装的扩展。",
        "dlg_spread_msg": "把「%s」里的 %d 个商店扩展强制装进所有配置？\n\n%s\n\n提示：写入后需彻底重启 Chrome，并打开对应账户窗口才会开始下载生效。",
        "dlg_unpack_err_title": "解包失败",
        "dlg_unpack_need_file": "这一条只能从 .crx 或文件夹来，光有 ID 解不了包",
        "dlg_local_need_sel": "在本地扩展列表里选中一个扩展。",
        "dlg_all_done_title": "齐了",
        "dlg_all_done_msg": "所有配置都已经有这个扩展了。",
        "dlg_opened_1_title": "开了 1 个窗口",
        "dlg_opened_1_msg": "把下面这个拖进窗口里的扩展页：\n%s",
        "dlg_opened_n_title": "开了 %d 个窗口",
        "dlg_opened_n_msg": "每个窗口对应一个还缺它的配置。\n\n把下面这个，逐个拖进每个窗口的扩展页：\n%s\n\n同一个文件可以拖很多次。",
        "dlg_local_del_msg": "从本地池移除 %s？\n（packs 里的文件保留，不会删）",
        "err_unknown_file": "认不出来：只支持 .crx / 解包目录 / 商店链接 / 32 位 ID",
        "dlg_startup_err": "启动失败",
    },
    "en": {
        "app_title": "ExtKeeper — Multi-Profile Chrome Extension Sync & Manager",
        "app_short": "ExtKeeper",
        "h1": "ExtKeeper",
        "subtitle": "  Chrome Extension Sync",
        "btn_refresh": "Refresh",
        "btn_lang_switch": "中文",
        "bar_prefix": "Baseline",
        "bar_suffix": "→ all profiles",
        "btn_spread": "Spread All",
        "chk_auto_sync": "Auto-Sync",
        "chk_builtin": "Built-in",
        "btn_kill_chrome": "Close Chrome",
        "btn_clear": "Clear Policies",
        "left_title": "Extensions",
        "left_count": "%d total",
        "left_legend": "● Installed  ◐ Pending Open  ○ Missing · Snapshot only; open Chrome to apply",
        "col_ext": "Extension",
        "col_id": "ID",
        "col_pol": "Policy",
        "tag_force": "Force",
        "tag_normal": "Normal",
        "tag_removed": "Remove",
        "tag_blocked": "Block",
        "unlanded": "Pending · ",
        "drop_t1_dnd": "Drop .crx file here",
        "drop_t2_dnd": "Click to browse · Folder OK",
        "drop_t1_click": "Click to select .crx package",
        "drop_t2_click": "Install tkinterdnd2 for drag & drop",
        "drop_t3": "Auto-detects Store vs. Local",
        "btn_lookup": "Lookup",
        "picked_empty": "No extension selected — drop, browse, or click a row",
        "picked_installed": "Installed %d / %d",
        "unnamed": "(Unnamed)",
        "ch_a": "Channel A · Web Store",
        "btn_force": "① Force-Install to All",
        "btn_normal": "② Auto-Install (Allow Disable)",
        "btn_remove": "③ Remove from Policy",
        "ch_b": "Channel B · Local / Private",
        "btn_local_del": "Del",
        "btn_local_add": "④ Unpack & Register",
        "btn_open_pages": "⑤ Open Pages for Missing",
        "log_title": "Log",
        "btn_elevate": "Run as Admin",
        "chk_autostart": "Auto-Start",
        "chk_bg_mode": "Start in Tray",
        "btn_hide_tray": "Hide to Tray",
        "pill_profiles": "%d Profiles",
        "pill_policies": "%d Policies",
        "pill_missing": "%d Missing",
        "pill_local": "%d Local",
        "pill_chrome_on": "Chrome On",
        "pill_chrome_off": "Chrome Off",
        "pill_non_admin": "UAC Needed",
        "pill_hklm_conflict": "HKLM Conflict",
        "tip_auto_sync_on": "Auto-Sync On",
        "kind_store": "Store",
        "kind_local": "Local",
        "kind_folder": "Folder",
        "kind_checking": "Checking…",
        "kind_failed": "Lookup Failed",
        "kind_policy_force": "Policy·Force",
        "kind_policy_normal": "Policy·Normal",
        "kind_policy_removed": "Policy·Remove",
        "kind_installed": "Installed",
        "kind_unknown": "Unknown",
        "tray_show": "Open ExtKeeper",
        "tray_hide": "Hide Window",
        "tray_refresh": "Refresh Status",
        "tray_kill_chrome": "Close All Chrome",
        "tray_sync": "Sync Now (%s)",
        "tray_sync_none": "Sync Now (No Baseline)",
        "tray_autostart": "Launch at Startup",
        "tray_bg_mode": "Start in Background",
        "tray_auto_sync": "Background Auto-Sync",
        "tray_lang_switch": "切换为简体中文",
        "tray_quit": "Exit ExtKeeper",
        "notify_bg_started": "Running in the system tray. Click the tray icon to open.",
        "notify_bg_hidden": "Minimized to the system tray. Click icon to open or right-click to exit.",
        "notify_no_source": "No baseline profile available for sync.",
        "notify_sync_clean": "All Web Store extensions in '%s' are already synced.",
        "notify_sync_done": "Synced %d new extension(s) (%s)",
        "notify_restart_chrome": ". Restart Chrome and open each profile window to apply.",
        "log_save_err": "Failed to save settings: %s",
        "log_autostart_on": "Auto-start enabled (%s)",
        "log_autostart_off": "Auto-start disabled.",
        "log_autostart_err": "Failed to update auto-start: %s",
        "log_need_admin": "Running as standard user: policy writes will auto-prompt UAC, or click 'Run as Admin'.",
        "log_hklm_conflict": "Detected legacy HKLM Chrome extension policies overriding HKCU; any install/remove/clear action will clean them automatically.",
        "log_uac_cancelled": "Administrator elevation cancelled; policy unchanged.",
        "log_policy_err": "Policy operation failed: %s",
        "dlg_policy_err_title": "Policy Write Failed",
        "dlg_policy_err_detail": "Failed to write Chrome Group Policy:\n%s\n\nPlease check:\n1. Click 'Yes' when the Windows UAC prompt appears\n2. Try clicking 'Run as Admin' at the bottom-right first\n3. Check if antivirus / security software blocked registry access",
        "dlg_store_missing_title": "Cannot Install via Web Store Policy",
        "dlg_store_missing_msg": "%s was not found on the Chrome Web Store.\n\nOn non-domain Windows PCs, Chrome blocks force-installing non-store extensions via policy.\nPlease use Channel B (Local / Private) below:\n④ Unpack & Register → ⑤ Open Pages for Missing, then drag & drop.",
        "dlg_store_net_title": "Web Store Check Failed",
        "dlg_store_net_msg": "Could not reach Chrome Web Store (clients2.google.com) while checking %s.\n\nPlease check your network/proxy. Without Web Store access, Chrome cannot download the extension even if policy is written.\nWrite the policy anyway?",
        "dlg_apply_ok_title": "Policy Saved (Open Profiles to Apply)",
        "dlg_apply_ok_chrome_on": "%s\n\n[How to Apply]\n1. Chrome is running: click 'Close Chrome' to exit all background processes, then reopen Chrome.\n2. Open each target Chrome profile window (unopened profiles won't download/remove in the background) and wait ~5–20s.\n3. Newly installed icons appear under the 🧩 Extensions menu. Click 'Refresh' here afterwards.\n\n[If Install Fails / Doesn't Take Effect, Check]\n· Network/proxy can reach clients2.google.com\n· Extension is still listed on Chrome Web Store (non-store CRX must use Channel B)\n· Open chrome://policy in Chrome and click 'Reload policies'",
        "dlg_apply_ok_chrome_off": "%s\n\n[How to Apply]\n1. Launch Chrome and open each target profile window (unopened profiles won't download/remove in the background).\n2. Wait ~5–20s per profile; newly installed icons appear under the 🧩 Extensions menu.\n3. Click 'Refresh' here afterwards to update status.\n\n[If Install Fails / Doesn't Take Effect, Check]\n· Network/proxy can reach clients2.google.com\n· Extension is still listed on Chrome Web Store (non-store CRX must use Channel B)\n· Open chrome://policy in Chrome and click 'Reload policies'",
        "dlg_need_admin_title": "Administrator Required",
        "dlg_need_admin_msg": "Writing Chrome Group Policy requires Administrator privileges (Access Denied).\n\nRelaunch ExtKeeper as Administrator now?",
        "log_removed_all": "Global removal policy set for %s (will uninstall from all profiles on Chrome restart).",
        "log_removed_cancel": "Removed global uninstall policy for %s.",
        "dlg_remove_choice_msg": "How would you like to remove %s?\n\n· Yes: Also set Chrome 'removed' policy to uninstall manually installed copies across ALL profiles (can still reinstall later)\n· No: Only remove from managed policy (uninstalls policy-managed copies only)",
        "dlg_remove_manual_msg": "%s is manually installed (not in policy).\n\nSet Chrome 'removed' policy so it uninstalls from all profiles on next Chrome restart? (Can still reinstall later)",
        "dlg_remove_undo_msg": "%s currently has a global 'removed' policy.\n\nRemove that uninstall policy so it can be installed normally again?",
        "log_bg_on": "Start in Tray enabled: launches silently into tray and hides on close.",
        "log_bg_off": "Start in Tray disabled: shows window on launch and exits on close.",
        "log_auto_sync_on": "Auto-Sync enabled: checking '%s' every minute for new Web Store extensions.",
        "log_auto_sync_off": "Auto-Sync disabled.",
        "log_source_set": "Baseline profile set to: %s (%s)",
        "log_sync_clean": "Checked '%s': all Web Store extensions are already in policy.",
        "log_sync_added": "Auto-Sync: added %d extension(s) from '%s' (%s).",
        "log_lang_changed": "Interface language switched to: English",
        "log_copied_id": "Copied ID: %s",
        "log_picked": "Detected %s  %s  [%s]",
        "log_checking": "Checking Chrome Web Store…",
        "log_not_in_store": "Not found on Web Store — use Channel B.",
        "log_dropped": "Loaded: %s",
        "log_failed": "Error: %s",
        "log_chrome_running": "Chrome is running — click 'Close Chrome' to exit completely, reopen Chrome, and open each profile window to trigger install/removal (5–20s; new icons appear under the 🧩 Extensions menu).",
        "log_apply_tip": "Tip: After launching Chrome, open each target profile window to trigger install/removal (unopened profiles won't download in background; takes ~5–20s; new icons appear under the 🧩 Extensions menu).",
        "log_chrome_not_running": "Chrome is not currently running.",
        "log_chrome_killed": "Closed all Chrome windows and background processes. Reopen Chrome and open each target profile window to apply policies.",
        "log_chrome_kill_left": "Attempted to close Chrome, but some processes remain (try running as Admin).",
        "log_chrome_kill_err": "Failed to close Chrome: %s",
        "dlg_kill_chrome_msg": "Close all Chrome windows and background processes?\n\nExtKeeper will try a graceful close first to preserve tabs, then force-end remaining background processes.",
        "log_forced": "Force-installed: %s",
        "log_normal": "Set to normal_installed (user toggleable): %s",
        "log_removed": "Removed from policy: %s",
        "log_removed_hint": "Policy-managed installs uninstall on next Chrome restart; manual installs may stay.",
        "log_cleared": "All extension policies cleared.",
        "log_spread": "Spread %d extension(s) to all profiles.",
        "log_unpacked": "Unpacked to packs\\%s",
        "log_allowed_src": "Allowed file:// install sources so Chrome accepts local .crx drops.",
        "log_unpack_err": "Unpack failed: %s",
        "log_missing_profs": "Missing in %d profile(s): %s",
        "log_drag_target": "Drop this file: %s",
        "log_local_removed": "Removed %s from local pool",
        "etc_more": " etc.",
        "sep_list": ", ",
        "dlg_pick_title": "Select Chrome Extension Package (.crx)",
        "dlg_crx_filter": "Chrome Extension",
        "dlg_all_filter": "All Files",
        "dlg_autostart_err": "Auto-Start Error",
        "dlg_no_source_title": "No Baseline Profile",
        "dlg_no_source_msg": "No available Chrome profiles detected.",
        "dlg_unrecognized": "Unrecognized Input",
        "dlg_paste_hint": "Paste a Chrome Web Store URL or a 32-character extension ID.",
        "dlg_need_title": "Select an Extension",
        "dlg_need_msg": "Drop a .crx file, paste a URL / ID, or click a row in the table first.",
        "dlg_confirm": "Confirm",
        "dlg_force_msg": "Force-install %s\ninto all %d Chrome profiles?\n\n· Force-installed extensions cannot be disabled/removed manually and display 'Managed by your organization'\n· Restart Chrome completely and open each profile window to trigger download (~5–20s; icons appear under the 🧩 menu)",
        "dlg_clear_msg": "Clear all extension policies?\nOnly ExtensionInstallForcelist, ExtensionSettings, and ExtensionInstallSources will be removed.",
        "dlg_spread_none_title": "Nothing to Spread",
        "dlg_spread_none_msg": "This profile has no Chrome Web Store extensions.",
        "dlg_spread_msg": "Force-install %d Web Store extension(s) from '%s' to all profiles?\n\n%s\n\nTip: Restart Chrome completely and open each profile window to trigger download.",
        "dlg_unpack_err_title": "Unpack Failed",
        "dlg_unpack_need_file": "Requires a .crx file or unpacked folder; an extension ID alone cannot be unpacked.",
        "dlg_local_need_sel": "Select an extension from the local pool list first.",
        "dlg_all_done_title": "All Set",
        "dlg_all_done_msg": "All profiles already have this extension installed.",
        "dlg_opened_1_title": "Opened 1 Window",
        "dlg_opened_1_msg": "Drag and drop this file into the opened chrome://extensions page:\n%s",
        "dlg_opened_n_title": "Opened %d Windows",
        "dlg_opened_n_msg": "Each window corresponds to a profile missing this extension.\n\nDrag and drop this file into each extensions page:\n%s\n\nYou can drop the same file multiple times.",
        "dlg_local_del_msg": "Remove %s from the local pool?\n(Files inside packs\\ will be kept)",
        "err_unknown_file": "Unsupported input: only .crx, unpacked folder, Web Store URL, or 32-char ID.",
        "dlg_startup_err": "Startup Failed",
    },
}


def detect_system_lang():
    """检测 Windows 系统界面语言：中文返回 'zh'，其他返回 'en'"""
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        # PRIMARYLANGID == 0x04 为中文（简体/繁体/港澳台新加坡）
        return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
    except Exception:
        return "zh"


def resolve_lang(mode):
    m = (mode or "auto").lower()
    if m in ("zh", "en"):
        return m
    return detect_system_lang()


def tr(lang, key, *args):
    table = I18N.get(lang, I18N["zh"])
    txt = table.get(key, I18N["zh"].get(key, key))
    if args:
        try:
            return txt % args
        except Exception:
            return txt
    return txt


# 保持顶层常量兼容（按当前系统语言初始化，用于外部脚本或异常兜底）
_BOOT_LANG = detect_system_lang()
APP_TITLE = tr(_BOOT_LANG, "app_title")
APP_SHORT_TITLE = tr(_BOOT_LANG, "app_short")


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
    """确保程序图标 extkeeper.ico 存在（优先读取 exe 同目录或内嵌图标，缺失时自动绘制）"""
    if os.path.exists(ICON_FILE):
        return ICON_FILE
    if os.path.exists(BUNDLED_ICON_FILE):
        return BUNDLED_ICON_FILE
    try:
        from PIL import Image, ImageDraw
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        pad = 14
        d.rounded_rectangle(
            (pad, pad, size - pad, size - pad),
            radius=54,
            fill=(37, 99, 235, 255),
        )
        d.rounded_rectangle(
            (pad + 6, pad + 6, size - pad - 6, size - pad - 6),
            radius=48,
            outline=(96, 165, 250, 140),
            width=4,
        )
        bx0, by0, bx1, by1 = 68, 86, 174, 192
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=20, fill=(255, 255, 255, 255))
        d.ellipse((100, 56, 142, 98), fill=(255, 255, 255, 255))
        d.ellipse((158, 118, 200, 160), fill=(255, 255, 255, 255))
        d.ellipse((50, 118, 90, 158), fill=(37, 99, 235, 255))
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
IDI_APPLICATION = 32512
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
SW_SHOWNORMAL = 1
INFINITE = 0xFFFFFFFF


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("fMask", wt.ULONG),
        ("hwnd", wt.HWND),
        ("lpVerb", wt.LPCWSTR),
        ("lpFile", wt.LPCWSTR),
        ("lpParameters", wt.LPCWSTR),
        ("lpDirectory", wt.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wt.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wt.LPCWSTR),
        ("hkeyClass", wt.HKEY),
        ("dwHotKey", wt.DWORD),
        ("hIconOrMonitor", wt.HANDLE),
        ("hProcess", wt.HANDLE),
    ]


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
shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
shell32.ShellExecuteExW.restype = wt.BOOL
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wt.BOOL
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD

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
    """零第三方依赖的 Windows 原生系统托盘（支持中英文菜单）"""

    MENU_SHOW = 1001
    MENU_REFRESH = 1002
    MENU_SYNC_NOW = 1003
    MENU_AUTOSTART = 1004
    MENU_BG_MODE = 1005
    MENU_AUTO_SYNC = 1006
    MENU_LANG = 1007
    MENU_KILL_CHROME = 1008
    MENU_QUIT = 1099

    def __init__(self, app, icon_path=""):
        self.app = app
        self.icon_path = icon_path
        self.hwnd = None
        self.hicon = None
        self._own_icon = False
        self._tooltip = APP_SHORT_TITLE
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
        nid.szTip = (self._tooltip or APP_SHORT_TITLE)[:127]
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
        self._tooltip = (text or APP_SHORT_TITLE)[:127]
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
            nid.szInfoTitle = (title or APP_SHORT_TITLE)[:63]
            nid.szInfo = (message or "")[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def _show_menu(self):
        st = self.app.get_tray_state()
        lang = st["lang"]
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            show_label = tr(lang, "tray_hide" if st["visible"] else "tray_show")
            user32.AppendMenuW(menu, MF_STRING, self.MENU_SHOW, show_label)
            user32.SetMenuDefaultItem(menu, self.MENU_SHOW, 0)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_REFRESH, tr(lang, "tray_refresh"))
            user32.AppendMenuW(menu, MF_STRING, self.MENU_KILL_CHROME, tr(lang, "tray_kill_chrome"))
            sync_flag = MF_STRING if st["has_source"] else (MF_STRING | MF_GRAYED)
            sync_txt = (
                tr(lang, "tray_sync", st["source_label"])
                if st["has_source"]
                else tr(lang, "tray_sync_none")
            )
            user32.AppendMenuW(menu, sync_flag, self.MENU_SYNC_NOW, sync_txt)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if st["autostart"] else 0),
                self.MENU_AUTOSTART,
                tr(lang, "tray_autostart"),
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if st["start_in_bg"] else 0),
                self.MENU_BG_MODE,
                tr(lang, "tray_bg_mode"),
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if st["auto_sync"] else 0),
                self.MENU_AUTO_SYNC,
                tr(lang, "tray_auto_sync"),
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_LANG, tr(lang, "tray_lang_switch"))
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.MENU_QUIT, tr(lang, "tray_quit"))

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
        elif cmd == self.MENU_KILL_CHROME:
            self.app.call_ui(self.app.do_kill_chrome)
        elif cmd == self.MENU_SYNC_NOW:
            self.app.call_ui(self.app.sync_from_source, True)
        elif cmd == self.MENU_AUTOSTART:
            self.app.call_ui(self.app.toggle_autostart_from_tray)
        elif cmd == self.MENU_BG_MODE:
            self.app.call_ui(self.app.toggle_bg_from_tray)
        elif cmd == self.MENU_AUTO_SYNC:
            self.app.call_ui(self.app.toggle_auto_sync_from_tray)
        elif cmd == self.MENU_LANG:
            self.app.call_ui(self.app.toggle_language)
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
        raise ValueError("Invalid CRX file (missing Cr24 magic header)")
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
            raise ValueError("crx_id not found in CRX3 header")
        eid = id_from_bytes(crx_id)
    elif ver == 2:
        pklen = struct.unpack("<I", data[8:12])[0]
        siglen = struct.unpack("<I", data[12:16])[0]
        pubkey = data[16:16 + pklen]
        payload = data[16 + pklen + siglen:]
        eid = id_from_bytes(hashlib.sha256(pubkey).digest()[:16])
    else:
        raise ValueError("Unsupported CRX version: %d" % ver)

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
        raise ValueError("manifest.json not found in folder")
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
        raise ValueError("Cannot derive extension ID (missing manifest key and folder name is not an ID)")
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

def _profile_display_name(pdir, meta):
    """优先取 Google 账户名，其次快捷方式名/配置名，最后回退目录名"""
    if not isinstance(meta, dict):
        return pdir
    for key in ("gaia_name", "gaia_given_name", "shortcut_name", "name"):
        val = (meta.get(key) or "").strip()
        if val:
            return val
    return pdir


def list_profiles():
    out = []
    info = {}
    order = []
    try:
        with open(os.path.join(CHROME_DATA, "Local State"), encoding="utf-8") as f:
            prof = json.load(f).get("profile", {}) or {}
            info = prof.get("info_cache", {}) or {}
            order = prof.get("profiles_order", []) or []
    except Exception:
        pass
    if not os.path.isdir(CHROME_DATA):
        return out
    order_idx = {d: i for i, d in enumerate(order)}
    for d in os.listdir(CHROME_DATA):
        p = os.path.join(CHROME_DATA, d)
        if not os.path.isdir(p) or d in ("System Profile", "Guest Profile", "Guest Profile 2"):
            continue
        if not (os.path.exists(os.path.join(p, "Preferences"))
                or os.path.exists(os.path.join(p, "Secure Preferences"))):
            continue
        out.append((d, _profile_display_name(d, info.get(d, {}))))
    out.sort(key=lambda x: (
        0 if x[0] in order_idx else 1,
        order_idx.get(x[0], 0),
        x[0] != "Default",
        x[0],
    ))
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
            if not isinstance(v, dict) or not v:
                continue
            loc = v.get("location")
            path = v.get("path", "") or ""
            man = v.get("manifest", {}) or {}
            # 过滤已卸载后留下的空壳条目（无 location、无 path、无 manifest）
            if loc is None and not path and not man:
                continue
            nm = man.get("name", "") or ""
            if nm.startswith("__MSG_"):
                nm = ""
            if eid in res and res[eid]["raw_name"]:
                continue
            res[eid] = {
                "name": nm or eid[:8],
                "raw_name": nm,
                "version": man.get("version", "") or "",
                "location": loc,
                "path": path,
            }
    return res


# ============================================================ 组策略与权限提权

class ElevationCancelled(Exception):
    """用户取消了 UAC 提权授权"""


def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def can_write_policies():
    """检测当前进程是否拥有写入 HKCU Chrome 组策略键的权限"""
    for sub in (FORCELIST, POLICY, r"Software\Policies\Google", r"Software\Policies"):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_ALL_ACCESS)
            winreg.CloseKey(k)
            return True
        except FileNotFoundError:
            continue
        except PermissionError:
            return False
        except OSError:
            return False
    return is_admin()


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


def _delete_value_if_exists(root, sub, value_name):
    try:
        k = winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteValue(k, value_name)
    except FileNotFoundError:
        pass
    finally:
        winreg.CloseKey(k)


def _del_tree_in(root, sub):
    try:
        k = winreg.OpenKey(root, sub, 0, winreg.KEY_ALL_ACCESS)
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
        _del_tree_in(root, sub + "\\" + s)
    try:
        winreg.DeleteKey(root, sub)
    except FileNotFoundError:
        pass


def del_tree(sub):
    _del_tree_in(winreg.HKEY_CURRENT_USER, sub)


def has_hklm_ext_policies():
    """检测 HKLM 下是否存在会覆盖 HKCU 的旧扩展策略（仅检查扩展相关三项）"""
    for sub in (FORCELIST, EXTSET, INSTALL_SOURCES):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
            winreg.CloseKey(k)
            return True
        except OSError:
            pass
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY)
        try:
            for vname in ("ExtensionSettings", "ExtensionInstallSources"):
                try:
                    winreg.QueryValueEx(k, vname)
                    return True
                except OSError:
                    pass
        finally:
            winreg.CloseKey(k)
    except OSError:
        pass
    return False


def clean_hklm_ext_policies():
    """清理 HKLM 下冲突的扩展三项策略（绝不触碰 WebRTC/DNS 等其他策略）"""
    _del_tree_in(winreg.HKEY_LOCAL_MACHINE, FORCELIST)
    _del_tree_in(winreg.HKEY_LOCAL_MACHINE, EXTSET)
    _del_tree_in(winreg.HKEY_LOCAL_MACHINE, INSTALL_SOURCES)
    _delete_value_if_exists(winreg.HKEY_LOCAL_MACHINE, POLICY, "ExtensionSettings")
    _delete_value_if_exists(winreg.HKEY_LOCAL_MACHINE, POLICY, "ExtensionInstallSources")


def _read_forcelist_entries(root=winreg.HKEY_CURRENT_USER):
    """返回 {eid: full_value}，保留原始 update_url"""
    out = {}
    try:
        k = winreg.OpenKey(root, FORCELIST)
    except OSError:
        return out
    try:
        for _, v in _enum_values(k):
            raw = str(v).strip()
            eid = raw.split(";")[0].strip()
            if eid:
                out[eid] = raw
    finally:
        winreg.CloseKey(k)
    return out


def read_forcelist():
    """读取当前强制安装列表（合并展示 HKCU 与可能存在的 HKLM 冲突项）"""
    out = {}
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            k = winreg.OpenKey(root, FORCELIST)
        except OSError:
            continue
        try:
            for n, v in _enum_values(k):
                eid = str(v).split(";")[0].strip()
                if eid and eid not in out:
                    out[eid] = str(n)
        finally:
            winreg.CloseKey(k)
    return out


def _read_extsettings_dict(root=winreg.HKEY_CURRENT_USER):
    """读取 Chrome ExtensionSettings（兼容官方 REG_SZ JSON 值与旧子键结构）"""
    merged = {}
    # 1. 先读旧子键结构
    try:
        k = winreg.OpenKey(root, EXTSET)
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                item = {}
                try:
                    sk = winreg.OpenKey(k, sub)
                    try:
                        for vn, vv in _enum_values(sk):
                            item[vn] = vv
                    finally:
                        winreg.CloseKey(sk)
                except OSError:
                    pass
                if item:
                    merged[sub] = item
        finally:
            winreg.CloseKey(k)
    except OSError:
        pass

    # 2. 再读官方 REG_SZ JSON 字符串值（优先级更高，覆盖同名项）
    try:
        pk = winreg.OpenKey(root, POLICY)
        try:
            raw, _ = winreg.QueryValueEx(pk, "ExtensionSettings")
            if isinstance(raw, str) and raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for eid, cfg in parsed.items():
                        if isinstance(cfg, dict):
                            merged[eid] = cfg
        except (OSError, ValueError, TypeError):
            pass
        finally:
            winreg.CloseKey(pk)
    except OSError:
        pass

    return merged


def _write_extsettings_dict(data):
    """以 Chrome 官方标准 REG_SZ JSON 格式写入 ExtensionSettings，并清理冲突子键"""
    _del_tree_in(winreg.HKEY_CURRENT_USER, EXTSET)
    clean = {k: v for k, v in (data or {}).items() if isinstance(v, dict) and v}
    if not clean:
        _delete_value_if_exists(winreg.HKEY_CURRENT_USER, POLICY, "ExtensionSettings")
        return
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, POLICY, 0, winreg.KEY_ALL_ACCESS)
    try:
        winreg.SetValueEx(
            k, "ExtensionSettings", 0, winreg.REG_SZ,
            json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        )
    finally:
        winreg.CloseKey(k)


def write_forcelist(ids, preserve_urls=None):
    """写入 HKCU ExtensionInstallForcelist；若列表为空则清理该键"""
    existing = _read_forcelist_entries(winreg.HKEY_CURRENT_USER)
    if preserve_urls:
        existing.update(preserve_urls)
    unique_ids = []
    for eid in ids:
        eid = (eid or "").strip()
        if eid and eid not in unique_ids:
            unique_ids.append(eid)
    if not unique_ids:
        _del_tree_in(winreg.HKEY_CURRENT_USER, FORCELIST)
        return
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, FORCELIST, 0, winreg.KEY_ALL_ACCESS)
    try:
        for n, _ in _enum_values(k):
            try:
                winreg.DeleteValue(k, n)
            except OSError:
                pass
        for idx, eid in enumerate(unique_ids, 1):
            val = existing.get(eid) or (eid + ";" + STORE_URL)
            winreg.SetValueEx(k, str(idx), 0, winreg.REG_SZ, val)
    finally:
        winreg.CloseKey(k)


def read_extsettings():
    """返回 {eid: installation_mode}（同时读取 HKCU 与 HKLM）"""
    out = {}
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for eid, cfg in _read_extsettings_dict(root).items():
            if eid not in out:
                out[eid] = cfg.get("installation_mode", "?")
    return out


def set_normal_installed(eid):
    cur = _read_extsettings_dict(winreg.HKEY_CURRENT_USER)
    cur[eid] = {
        "installation_mode": "normal_installed",
        "update_url": STORE_URL,
    }
    _write_extsettings_dict(cur)


def set_removed_policy(eid):
    cur = _read_extsettings_dict(winreg.HKEY_CURRENT_USER)
    cur[eid] = {"installation_mode": "removed"}
    _write_extsettings_dict(cur)


def del_extsetting_item(eid):
    cur = _read_extsettings_dict(winreg.HKEY_CURRENT_USER)
    if eid in cur:
        del cur[eid]
    _write_extsettings_dict(cur)


def allow_local_sources():
    """允许从 file:// 装 crx（Chrome 默认禁止非商店来源），清理父键同名旧值防冲突"""
    _delete_value_if_exists(winreg.HKEY_CURRENT_USER, POLICY, "ExtensionInstallSources")
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, INSTALL_SOURCES, 0,
                           winreg.KEY_ALL_ACCESS)
    try:
        winreg.SetValueEx(k, "1", 0, winreg.REG_SZ, "file:///*")
        winreg.SetValueEx(k, "2", 0, winreg.REG_SZ, "http://localhost/*")
    finally:
        winreg.CloseKey(k)


def clear_all_policies_direct():
    """清空 HKCU 与 HKLM 下的扩展三项策略（不动其他浏览器策略）"""
    _del_tree_in(winreg.HKEY_CURRENT_USER, FORCELIST)
    _del_tree_in(winreg.HKEY_CURRENT_USER, EXTSET)
    _del_tree_in(winreg.HKEY_CURRENT_USER, INSTALL_SOURCES)
    _delete_value_if_exists(winreg.HKEY_CURRENT_USER, POLICY, "ExtensionSettings")
    _delete_value_if_exists(winreg.HKEY_CURRENT_USER, POLICY, "ExtensionInstallSources")
    if has_hklm_ext_policies():
        clean_hklm_ext_policies()


def _execute_policy_ops_direct(ops):
    """直接执行一组策略变更操作"""
    for op in ops:
        act = op.get("op")
        if act == "write_forcelist":
            write_forcelist(op.get("ids", []), op.get("preserve_urls"))
        elif act == "set_normal":
            set_normal_installed(op["id"])
        elif act == "set_removed":
            set_removed_policy(op["id"])
        elif act == "del_extset":
            del_extsetting_item(op["id"])
        elif act == "allow_local_sources":
            allow_local_sources()
        elif act == "clear_all":
            clear_all_policies_direct()
        elif act == "clean_hklm":
            if has_hklm_ext_policies():
                clean_hklm_ext_policies()


def _run_elevated_policy_ops(ops, hwnd=None):
    """通过 UAC 提权子进程原子执行策略变更并等待结果"""
    req_fd, req_path = tempfile.mkstemp(prefix="extkeeper_req_", suffix=".json")
    res_fd, res_path = tempfile.mkstemp(prefix="extkeeper_res_", suffix=".json")
    os.close(req_fd)
    os.close(res_fd)
    try:
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump({"ops": ops}, f, ensure_ascii=False)

        if IS_FROZEN:
            exe = os.path.abspath(sys.executable)
            params = '--apply-policy "%s" "%s"' % (req_path, res_path)
        else:
            exe = get_pythonw_path()
            params = '"%s" --apply-policy "%s" "%s"' % (
                os.path.abspath(__file__), req_path, res_path
            )

        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = hwnd
        sei.lpVerb = "runas"
        sei.lpFile = exe
        sei.lpParameters = params
        sei.lpDirectory = HERE
        sei.nShow = SW_HIDE

        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = kernel32.GetLastError()
            if err == ERROR_CANCELLED:
                raise ElevationCancelled()
            raise OSError(err, "ShellExecuteExW failed (code %d)" % err)

        if sei.hProcess:
            try:
                kernel32.WaitForSingleObject(sei.hProcess, 30000)
            finally:
                kernel32.CloseHandle(sei.hProcess)

        if os.path.exists(res_path) and os.path.getsize(res_path) > 0:
            with open(res_path, encoding="utf-8") as f:
                res = json.load(f)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "Elevated policy update failed")
            return
        raise RuntimeError("Elevated helper exited without writing result")
    finally:
        for fp in (req_path, res_path):
            try:
                os.remove(fp)
            except OSError:
                pass


def apply_policy_ops(ops, hwnd=None, allow_uac=True):
    """执行策略变更：优先直接写入；遇权限不足或 HKLM 冲突则自动按需走 UAC 提权执行"""
    full_ops = list(ops)
    need_hklm_clean = has_hklm_ext_policies() and not any(o.get("op") == "clear_all" for o in full_ops)
    if need_hklm_clean:
        full_ops.append({"op": "clean_hklm"})

    need_admin = (not can_write_policies()) or has_hklm_ext_policies()
    if not need_admin:
        try:
            _execute_policy_ops_direct(full_ops)
            return
        except PermissionError:
            pass

    if not allow_uac:
        raise PermissionError("Administrator privileges required to modify Chrome policies")
    _run_elevated_policy_ops(full_ops, hwnd=hwnd)


def _handle_apply_policy_cli(argv):
    """提权子进程专用入口：执行完 JSON 中的策略操作后立刻写结果并退出"""
    try:
        idx = argv.index("--apply-policy")
    except ValueError:
        return False
    if idx + 2 >= len(argv):
        return True
    req_path, res_path = argv[idx + 1], argv[idx + 2]
    try:
        with open(req_path, encoding="utf-8") as f:
            payload = json.load(f)
        _execute_policy_ops_direct(payload.get("ops", []))
        res = {"ok": True}
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    try:
        with open(res_path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception:
        pass
    return True


def relaunch_as_admin(hwnd=None):
    """以管理员身份重新启动主界面"""
    if IS_FROZEN:
        exe = os.path.abspath(sys.executable)
        params = "--show"
    else:
        exe = get_pythonw_path()
        params = '"%s" --show' % os.path.abspath(__file__)

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = 0
    sei.hwnd = hwnd
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = HERE
    sei.nShow = SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = kernel32.GetLastError()
        if err == ERROR_CANCELLED:
            return False
        raise OSError(err, "ShellExecuteExW failed (code %d)" % err)
    return True


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


def kill_chrome_processes():
    """先温和关闭 Chrome 窗口保留会话，再强制结束残留后台进程；返回是否已全部关闭"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if not chrome_running():
        return True
    try:
        subprocess.run(
            ["taskkill", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, timeout=5,
        )
    except Exception:
        pass
    for _ in range(15):
        if not chrome_running():
            return True
        time.sleep(0.1)
    try:
        subprocess.run(
            ["taskkill", "/IM", "chrome.exe", "/F", "/T"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, timeout=8,
        )
    except Exception:
        pass
    for _ in range(10):
        if not chrome_running():
            return True
        time.sleep(0.1)
    return not chrome_running()


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
    def __init__(self, root, start_hidden=False, launched_by_autostart=False,
                 icon_path="", lang_override=None, mutex_ref=None):
        self.root = root
        self.icon_path = icon_path
        self._mutex_ref = mutex_ref if mutex_ref is not None else [None]
        self.settings = load_settings()
        self.lang_mode = lang_override or self.settings.get("language", "auto")
        self.lang = resolve_lang(self.lang_mode)

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
        self._policy_writable = can_write_policies()
        self._hklm_conflict = has_hklm_ext_policies()

        self.show_builtin = tk.BooleanVar(value=False)
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.start_in_bg_var = tk.BooleanVar(value=bool(self.settings.get("start_in_background", False)))
        self.auto_sync_var = tk.BooleanVar(value=bool(self.settings.get("auto_sync", False)))

        root.title(self.t("app_title"))
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

        self._apply_language(refresh_data=False)
        self.refresh()

        if not self._policy_writable:
            self.log(self.t("log_need_admin"), "warn")
        if self._hklm_conflict:
            self.log(self.t("log_hklm_conflict"), "warn")

        if self._hidden:
            self.root.withdraw()
            if not launched_by_autostart:
                self.tray.notify(self.t("app_short"), self.t("notify_bg_started"))
        else:
            self.root.deiconify()

        self._schedule_polls()

    # -------------------------------------------------- 语言与翻译

    def t(self, key, *args):
        return tr(self.lang, key, *args)

    def set_language(self, mode, log_change=True):
        """设置语言模式（'auto' / 'zh' / 'en'）并即时刷新全界面"""
        self.lang_mode = mode if mode in ("auto", "zh", "en") else "auto"
        self.lang = resolve_lang(self.lang_mode)
        self._persist_settings()
        self._apply_language(refresh_data=True)
        if log_change:
            self.log(self.t("log_lang_changed"))

    def toggle_language(self):
        """在简体中文与 English 之间一键切换"""
        nxt = "en" if self.lang == "zh" else "zh"
        self.set_language(nxt, log_change=True)

    def _apply_language(self, refresh_data=True):
        """将当前语言应用到窗口标题及所有静态/动态控件"""
        self.root.title(self.t("app_title"))
        self.h1_lb.configure(text=self.t("h1"))
        self.sub_lb.configure(text=self.t("subtitle"))
        self.refresh_btn.configure(text=self.t("btn_refresh"))
        self.lang_btn.configure(text=self.t("btn_lang_switch"))

        self.bar_pre_lb.configure(text=self.t("bar_prefix"))
        self.bar_suf_lb.configure(text=self.t("bar_suffix"))
        self.spread_btn.configure(text=self.t("btn_spread"))
        self.auto_sync_chk.configure(text=self.t("chk_auto_sync"))
        self.builtin_chk.configure(text=self.t("chk_builtin"))
        self.kill_chrome_btn.configure(text=self.t("btn_kill_chrome"))
        self.clear_btn.configure(text=self.t("btn_clear"))

        self.left_title_lb.configure(text=self.t("left_title"))
        self.left_legend_lb.configure(text=self.t("left_legend"))
        self.lookup_btn.configure(text=self.t("btn_lookup"))

        self.ch_a_lb.configure(text=self.t("ch_a"))
        self.force_btn.configure(text=self.t("btn_force"))
        self.normal_btn.configure(text=self.t("btn_normal"))
        self.remove_btn.configure(text=self.t("btn_remove"))

        self.ch_b_lb.configure(text=self.t("ch_b"))
        self.local_del_btn.configure(text=self.t("btn_local_del"))
        self.local_add_btn.configure(text=self.t("btn_local_add"))
        self.open_pages_btn.configure(text=self.t("btn_open_pages"))

        self.log_title_lb.configure(text=self.t("log_title"))
        self.elevate_btn.configure(text=self.t("btn_elevate"))
        self.autostart_chk.configure(text=self.t("chk_autostart"))
        self.bg_mode_chk.configure(text=self.t("chk_bg_mode"))
        self.hide_tray_btn.configure(text=self.t("btn_hide_tray"))

        self._draw_drop()
        self._render_pending()

        if refresh_data:
            self.render()
            self.render_status()

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
            "lang": self.lang,
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
        self.settings["language"] = self.lang_mode
        try:
            save_settings(self.settings)
        except Exception as e:
            self.log(self.t("log_save_err", e), "warn")

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
            self.tray.notify(self.t("app_short"), self.t("notify_bg_hidden"))

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
                self.log(self.t("log_autostart_on", cmd))
            else:
                self.log(self.t("log_autostart_off"))
        except Exception as e:
            self.autostart_var.set(is_autostart_enabled())
            self.log(self.t("log_autostart_err", e), "warn")
            messagebox.showwarning(self.t("dlg_autostart_err"), str(e))

    def on_toggle_bg_mode(self):
        self._persist_settings()
        if self.start_in_bg_var.get():
            self.log(self.t("log_bg_on"))
        else:
            self.log(self.t("log_bg_off"))

    def on_toggle_auto_sync(self):
        self._persist_settings()
        src_dir, src_name = self._current_source_profile()
        if self.auto_sync_var.get():
            if not src_dir:
                self.auto_sync_var.set(False)
                self._persist_settings()
                messagebox.showinfo(self.t("dlg_no_source_title"), self.t("dlg_no_source_msg"))
                return
            self.log(self.t("log_auto_sync_on", src_name))
            self.sync_from_source(interactive=False)
        else:
            self.log(self.t("log_auto_sync_off"))

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
            self.log(self.t("log_source_set", src_name, src_dir))

    def _schedule_polls(self):
        if self._quitting:
            return
        self.root.after(STATUS_POLL_MS, self._poll_status)
        self.root.after(AUTO_SYNC_POLL_MS, self._poll_auto_sync)

    def _poll_status(self):
        if self._quitting:
            return
        try:
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
                self.tray.notify(self.t("app_short"), self.t("notify_no_source"))
            return 0
        exts = read_extensions(src_dir)
        store_ids = [
            e for e, i in exts.items()
            if i.get("location") in (1, 6, 7) and e not in BUILTIN_IDS
        ]
        cur_force = read_forcelist()
        cur_extset = read_extsettings()
        missing = [e for e in store_ids if e not in cur_force and e not in cur_extset]
        if not missing:
            if interactive:
                self.log(self.t("log_sync_clean", src_name))
                if self._hidden:
                    self.tray.notify(self.t("app_short"), self.t("notify_sync_clean", src_name))
            return 0

        new_ids = list(cur_force.keys()) + missing
        try:
            apply_policy_ops(
                [{"op": "write_forcelist", "ids": new_ids}],
                hwnd=self._hwnd(),
                allow_uac=interactive,
            )
        except ElevationCancelled:
            self.log(self.t("log_uac_cancelled"), "warn")
            return 0
        except Exception as e:
            if interactive:
                self.log(self.t("log_policy_err", e), "warn")
            return 0
        names = [exts[e]["name"] for e in missing[:3]]
        summary = self.t("sep_list").join(names) + (self.t("etc_more") if len(missing) > 3 else "")
        if self.lang == "en":
            self.log(self.t("log_sync_added", len(missing), src_name, summary))
        else:
            self.log(self.t("log_sync_added", src_name, len(missing), summary))
        self.refresh()
        if self._hidden or interactive:
            tip = self.t("notify_sync_done", len(missing), summary)
            if chrome_running():
                tip += self.t("notify_restart_chrome")
            self.tray.notify(self.t("app_short"), tip)
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
        self.h1_lb = tk.Label(head, text="", bg=C_BG, fg=C_TEXT, font=F_H1)
        self.h1_lb.pack(side="left")
        self.sub_lb = tk.Label(head, text="", bg=C_BG, fg=C_MUTED, font=F_SMALL)
        self.sub_lb.pack(side="left", pady=(5, 0))
        self.ver_lb = tk.Label(head, text="", bg=C_BG, fg=C_FAINT,
                               font=("Microsoft YaHei UI", 8))
        self.ver_lb.pack(side="left", padx=(10, 0), pady=(6, 0))
        self.lang_btn = ttk.Button(head, text="", style="Ghost.TButton", width=4,
                                   command=self.toggle_language)
        self.lang_btn.pack(side="right", padx=(2, 0))
        self.refresh_btn = ttk.Button(head, text="", style="Ghost.TButton", width=7,
                                      command=self.refresh)
        self.refresh_btn.pack(side="right")
        self.pill = tk.Label(head, text="", bg="#E8F5EE", fg=C_GREEN, font=F_SMALL,
                             padx=9, pady=4)
        self.pill.pack(side="right", padx=(0, 8), pady=(2, 0))

        bar = tk.Frame(outer, bg=C_BG)
        bar.pack(fill="x", pady=(10, 10))
        self.bar_pre_lb = tk.Label(bar, text="", bg=C_BG, fg=C_MUTED, font=F_SMALL)
        self.bar_pre_lb.pack(side="left")
        self.src_box = ttk.Combobox(bar, state="readonly", width=20, font=F_SMALL)
        self.src_box.pack(side="left", padx=5)
        self.src_box.bind("<<ComboboxSelected>>", self.on_source_changed)
        self.bar_suf_lb = tk.Label(bar, text="", bg=C_BG, fg=C_MUTED, font=F_SMALL)
        self.bar_suf_lb.pack(side="left")
        self.spread_btn = ttk.Button(bar, text="", command=self.do_spread)
        self.spread_btn.pack(side="left", padx=6)
        self.auto_sync_chk = ttk.Checkbutton(bar, text="", variable=self.auto_sync_var,
                                             command=self.on_toggle_auto_sync)
        self.auto_sync_chk.pack(side="left", padx=(6, 0))
        self.builtin_chk = ttk.Checkbutton(bar, text="", variable=self.show_builtin,
                                           command=self.render)
        self.builtin_chk.pack(side="left", padx=(10, 0))
        self.clear_btn = ttk.Button(bar, text="", style="Danger.TButton",
                                    command=self.do_clear)
        self.clear_btn.pack(side="right")
        self.kill_chrome_btn = ttk.Button(bar, text="", command=self.do_kill_chrome)
        self.kill_chrome_btn.pack(side="right", padx=(0, 6))

        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill="both", expand=True)

        # 右栏必须先 pack：左栏带 expand，后 pack 会把右栏挤出窗口
        rightcard = tk.Frame(body, bg=C_CARD, width=356, highlightbackground=C_BORDER,
                             highlightthickness=1)
        rightcard.pack(side="right", fill="y", padx=(12, 0))
        rightcard.pack_propagate(False)

        leftcard = tk.Frame(body, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
        leftcard.pack(side="left", fill="both", expand=True)

        lhead = tk.Frame(leftcard, bg=C_CARD)
        lhead.pack(fill="x", padx=14, pady=(11, 4))
        self.left_title_lb = tk.Label(lhead, text="", bg=C_CARD, fg=C_TEXT, font=F_BOLD)
        self.left_title_lb.pack(side="left")
        self.count_lb = tk.Label(lhead, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL)
        self.count_lb.pack(side="left", padx=8, pady=(2, 0))
        self.left_legend_lb = tk.Label(lhead, text="", bg=C_CARD, fg=C_FAINT, font=F_SMALL)
        self.left_legend_lb.pack(side="right")

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
        if HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)
            self.drop.dnd_bind("<<DropEnter>>", lambda e: self._set_hover(True))
            self.drop.dnd_bind("<<DropLeave>>", lambda e: self._set_hover(False))

        prow = tk.Frame(rightcard, bg=C_CARD)
        prow.pack(fill="x", padx=14)
        self.lookup_btn = ttk.Button(prow, text="", width=7, command=self.on_paste)
        self.lookup_btn.pack(side="right", padx=(6, 0))
        self.paste = ttk.Entry(prow, font=F_SMALL, width=10)
        self.paste.pack(side="left", fill="x", expand=True)
        self.paste.bind("<Return>", lambda e: self.on_paste())

        self.picked = tk.Label(rightcard, text="", bg=C_CARD, fg=C_FAINT, font=F_SMALL,
                               height=2, justify="left", anchor="w", wraplength=308)
        self.picked.pack(fill="x", padx=14, pady=(8, 4))

        self._divider(rightcard)

        self.ch_a_lb = tk.Label(rightcard, text="", bg=C_CARD, fg=C_GREEN, font=F_BOLD)
        self.ch_a_lb.pack(anchor="w", padx=14)
        self.force_btn = ttk.Button(rightcard, text="", style="Accent.TButton",
                                    command=self.do_force)
        self.force_btn.pack(fill="x", padx=14, pady=2)
        self.normal_btn = ttk.Button(rightcard, text="", command=self.do_normal)
        self.normal_btn.pack(fill="x", padx=14, pady=2)
        self.remove_btn = ttk.Button(rightcard, text="", style="Danger.TButton",
                                     command=self.do_remove)
        self.remove_btn.pack(fill="x", padx=14, pady=2)

        self._divider(rightcard)

        self.ch_b_lb = tk.Label(rightcard, text="", bg=C_CARD, fg=C_AMBER, font=F_BOLD)
        self.ch_b_lb.pack(anchor="w", padx=14)

        lrow = tk.Frame(rightcard, bg=C_CARD)
        lrow.pack(fill="x", padx=14)
        self.local_lb = tk.Listbox(lrow, height=2, font=F_SMALL, activestyle="none",
                                   exportselection=False, bg="#FCFDFE", fg=C_TEXT,
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=C_BORDER, borderwidth=0,
                                   selectbackground="#EAF0FB", selectforeground=C_TEXT)
        self.local_lb.pack(side="left", fill="x", expand=True)
        self.local_del_btn = ttk.Button(lrow, text="", width=5, command=self.do_local_del)
        self.local_del_btn.pack(side="left", padx=(6, 0), anchor="n")

        self.local_add_btn = ttk.Button(rightcard, text="", command=self.do_local_add)
        self.local_add_btn.pack(fill="x", padx=14, pady=(8, 3))
        self.open_pages_btn = ttk.Button(rightcard, text="", command=self.do_open_pages)
        self.open_pages_btn.pack(fill="x", padx=14, pady=2)

        # 底部记录卡片 + 后台/开机自启选项栏
        footcard = tk.Frame(outer, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
        footcard.pack(fill="x", pady=(12, 0))
        fh = tk.Frame(footcard, bg=C_CARD)
        fh.pack(fill="x", padx=14, pady=(6, 0))
        self.log_title_lb = tk.Label(fh, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL)
        self.log_title_lb.pack(side="left")
        self.hide_tray_btn = ttk.Button(
            fh, text="", style="CardGhost.TButton",
            command=lambda: self.hide_to_tray(notify_first=True),
        )
        self.hide_tray_btn.pack(side="right")
        self.elevate_btn = ttk.Button(
            fh, text="", style="CardGhost.TButton",
            command=self.do_relaunch_admin,
        )
        if not self._policy_writable:
            self.elevate_btn.pack(side="right", padx=(0, 8))
        self.bg_mode_chk = ttk.Checkbutton(
            fh, text="", style="Card.TCheckbutton",
            variable=self.start_in_bg_var, command=self.on_toggle_bg_mode,
        )
        self.bg_mode_chk.pack(side="right", padx=(0, 12))
        self.autostart_chk = ttk.Checkbutton(
            fh, text="", style="Card.TCheckbutton",
            variable=self.autostart_var, command=self.on_toggle_autostart,
        )
        self.autostart_chk.pack(side="right", padx=(0, 12))

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
            w = 304
        h = 84
        on = self._hovering
        c.create_rectangle(3, 3, w - 3, h - 3, outline=C_ACCENT if on else "#CBD5E1",
                           width=2, dash=(5, 4), fill="#F5F9FF" if on else C_CARD)
        if HAS_DND:
            t1, t2 = self.t("drop_t1_dnd"), self.t("drop_t2_dnd")
        else:
            t1, t2 = self.t("drop_t1_click"), self.t("drop_t2_click")
        c.create_text(w / 2, 25, text=t1, fill=C_TEXT,
                      font=("Microsoft YaHei UI", 11, "bold"))
        c.create_text(w / 2, 46, text=t2, fill=C_MUTED, font=F_SMALL)
        c.create_text(w / 2, 65, text=self.t("drop_t3"),
                      fill=C_FAINT, font=("Microsoft YaHei UI", 8))

    def _pick_file(self, event=None):
        p = filedialog.askopenfilename(
            title=self.t("dlg_pick_title"),
            filetypes=[(self.t("dlg_crx_filter"), "*.crx"), (self.t("dlg_all_filter"), "*.*")],
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
        mode = self.extset.get(eid)
        if eid in self.force or mode == "force_installed":
            kind_key = "kind_policy_force"
        elif mode == "normal_installed":
            kind_key = "kind_policy_normal"
        elif mode in ("removed", "blocked"):
            kind_key = "kind_policy_removed"
        elif entry and entry["info"].get("location") == 1:
            kind_key = "kind_store"
        else:
            kind_key = "kind_installed"
        self.set_pending(eid, name, ver, "table", kind_key, log_it=False)

    def _copy_id(self, event=None):
        item = self.tree.focus()
        eid = self.row_ids.get(item)
        if not eid:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(eid)
        self.log(self.t("log_copied_id", eid))

    # -------------------------------------------------- 数据与渲染

    def _hwnd(self):
        try:
            return int(self.root.winfo_id())
        except Exception:
            return None

    def _run_policy_ops(self, ops):
        """执行策略变更（必要时自动 UAC 提权），返回是否成功"""
        try:
            apply_policy_ops(ops, hwnd=self._hwnd(), allow_uac=True)
            return True
        except ElevationCancelled:
            self.log(self.t("log_uac_cancelled"), "warn")
            return False
        except Exception as e:
            self.log(self.t("log_policy_err", e), "warn")
            messagebox.showwarning(
                self.t("dlg_policy_err_title"),
                self.t("dlg_policy_err_detail", e),
            )
            return False

    def do_relaunch_admin(self):
        """释放单实例锁并以管理员身份重启主窗口"""
        self._persist_settings()
        hm = self._mutex_ref[0]
        if hm:
            try:
                kernel32.CloseHandle(hm)
            except Exception:
                pass
            self._mutex_ref[0] = None
        try:
            launched = relaunch_as_admin(hwnd=self._hwnd())
        except Exception as e:
            self._mutex_ref[0] = acquire_single_instance(wake_existing=False)
            self.log(self.t("log_policy_err", e), "warn")
            messagebox.showwarning(self.t("dlg_need_admin_title"), str(e))
            return
        if launched:
            self.quit_app()
        else:
            self._mutex_ref[0] = acquire_single_instance(wake_existing=False)
            self.log(self.t("log_uac_cancelled"), "warn")

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
        self._policy_writable = can_write_policies()
        self._hklm_conflict = has_hklm_ext_policies()
        self.autostart_var.set(is_autostart_enabled())

        prev_dir, _ = self._current_source_profile()
        target_dir = prev_dir or self.settings.get("sync_source", "")
        self.src_box["values"] = ["%s (%s)" % (n, d) for d, n in self.profiles]
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
        self._render_pending()

    def _build_status_parts(self):
        total = len(self.profiles)
        stuck = 0
        for eid in self.force:
            where = self.ext_map.get(eid, {}).get("where", {})
            if sum(1 for d, _ in self.profiles if d in where) < total:
                stuck += 1
        pol_ids = set(self.force.keys()) | set(self.extset.keys())
        parts = [self.t("pill_profiles", total), self.t("pill_policies", len(pol_ids))]
        if stuck:
            parts.append(self.t("pill_missing", stuck))
        if self.pool:
            parts.append(self.t("pill_local", len(self.pool)))
        if self._hklm_conflict:
            parts.append(self.t("pill_hklm_conflict"))
        if not self._policy_writable:
            parts.append(self.t("pill_non_admin"))
        run = chrome_running()
        parts.append(self.t("pill_chrome_on" if run else "pill_chrome_off"))
        return parts, run

    def _update_tray_tooltip(self):
        parts, _ = self._build_status_parts()
        tip = "%s - %s" % (self.t("app_short"), " · ".join(parts[:3]))
        if self.auto_sync_var.get():
            tip += " · " + self.t("tip_auto_sync_on")
        self.tray.set_tooltip(tip)

    def render_status(self):
        parts, run = self._build_status_parts()
        warn = run or self._hklm_conflict or (not self._policy_writable)
        self.pill.configure(text=" · ".join(parts),
                            bg="#FDF1E3" if warn else "#E8F5EE",
                            fg=C_AMBER if warn else C_GREEN)
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
        pol_w, id_w = 76, 118
        name_w = max(185, min(285, int(total * 0.27)))
        pw = max(56, (total - name_w - id_w - pol_w - 6) // n)
        return name_w, id_w, pw, pol_w

    def _dup_profile_names(self):
        counts = {}
        for _, n in self.profiles:
            counts[n] = counts.get(n, 0) + 1
        return {n for n, c in counts.items() if c > 1}

    def _apply_profile_headings(self, pw):
        dup = self._dup_profile_names()
        max_units = max(6, (pw - 8) // 6)
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

    def _policy_tag(self, eid):
        mode = self.extset.get(eid)
        if eid in self.force or mode == "force_installed":
            return self.t("tag_force")
        if mode == "normal_installed":
            return self.t("tag_normal")
        if mode == "removed":
            return self.t("tag_removed")
        if mode == "blocked":
            return self.t("tag_blocked")
        return ""

    def render(self):
        profs = [p[0] for p in self.profiles]
        cols = ["name", "id"] + ["p%d" % i for i in range(len(profs))] + ["pol"]
        self.tree.configure(columns=cols)
        name_w, id_w, pw, pol_w = self._col_widths()
        self.tree.heading("name", text=self.t("col_ext"))
        self.tree.column("name", width=name_w, anchor="w")
        self.tree.heading("id", text=self.t("col_id"))
        self.tree.column("id", width=id_w, anchor="w")
        self._apply_profile_headings(pw)
        self.tree.heading("pol", text=self.t("col_pol"))
        self.tree.column("pol", width=pol_w, anchor="center")

        for i in self.tree.get_children():
            self.tree.delete(i)
        self.row_ids = {}

        rows = []
        for eid, d in self.ext_map.items():
            loc = d["info"].get("location")
            tag = self._policy_tag(eid)
            if (loc in HIDDEN_LOC or eid in BUILTIN_IDS) and not tag and not self.show_builtin.get():
                continue
            mode = self.extset.get(eid)
            want_install = (eid in self.force) or (mode in ("force_installed", "normal_installed"))
            want_remove = mode in ("removed", "blocked")
            cells = []
            for p in profs:
                if p in d["where"]:
                    cells.append("✕" if want_remove else "●")
                else:
                    cells.append("◐" if want_install else "○")
            short = (eid[:8] + "…") if len(eid) > 8 else eid
            rows.append((d["name"], eid, short, cells, tag))

        for eid in list(self.force.keys()) + [e for e in self.extset if e not in self.force]:
            if eid in self.ext_map:
                continue
            mode = self.extset.get(eid)
            want_install = (eid in self.force) or (mode in ("force_installed", "normal_installed"))
            rows.append((self.t("unlanded") + eid[:8], eid, eid[:8] + "…",
                         ["◐" if want_install else "○"] * len(profs),
                         self._policy_tag(eid)))

        rows.sort(key=lambda r: r[0].lower())
        for idx, (name, eid, short, cells, tag) in enumerate(rows):
            iid = self.tree.insert("", "end", values=[name, short] + cells + [tag],
                                   tags=("odd",) if idx % 2 else ())
            self.row_ids[iid] = eid

        self.count_lb.configure(text=self.t("left_count", len(rows)))

    # -------------------------------------------------- 选择与异步商店检查

    def _render_pending(self):
        if not self.pending:
            self.picked.configure(text=self.t("picked_empty"), fg=C_FAINT)
            return
        p = self.pending
        eid = p["id"]
        kind_key = p.get("kind_key", "kind_unknown")
        kind_label = self.t(kind_key)
        where = len(self.ext_map.get(eid, {}).get("where", {}))
        color = {
            "kind_store": C_GREEN,
            "kind_local": C_AMBER,
            "kind_policy_force": C_ACCENT,
            "kind_policy_normal": C_GREEN,
            "kind_policy_removed": C_RED,
        }.get(kind_key, C_TEXT)
        line1 = "%s · %s  v%s" % (kind_label, p["name"] or self.t("unnamed"), p["version"] or "?")
        line1 = _fit_text(line1, 30)
        short_id = (eid[:14] + "…") if len(eid) > 14 else eid
        self.picked.configure(
            text="%s\n%s  ·  %s"
                 % (line1, short_id, self.t("picked_installed", where, len(self.profiles))),
            fg=color)

    def set_pending(self, eid, name, version, source, kind_key, log_it=True):
        self.pending = {
            "id": eid, "name": name, "version": version,
            "source": source, "kind_key": kind_key,
            "kind": self.t(kind_key),
        }
        self._render_pending()
        if log_it:
            self.log(self.t("log_picked", name or self.t("unnamed"), eid, self.t(kind_key)))

    def _query_store_async(self, eid, name, ver, source, local_hint=False):
        """后台线程查询商店是否存在，避免阻塞界面"""
        self._query_seq += 1
        seq = self._query_seq
        self.set_pending(eid, name, ver, source, "kind_checking", log_it=False)
        self.log(self.t("log_checking"))

        def worker():
            has = store_has(eid)
            kind_key = {True: "kind_store", False: "kind_local", None: "kind_failed"}[has]

            def finish():
                if seq != self._query_seq:
                    return
                self.set_pending(eid, name, ver, source, kind_key)
                if local_hint and kind_key == "kind_local":
                    self.log(self.t("log_not_in_store"))

            self.call_ui(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_path(self, p):
        self.log(self.t("log_dropped", p))
        try:
            if os.path.isdir(p):
                eid, name, ver = read_folder(p)
                self.set_pending(eid, name, ver, p, "kind_folder")
            elif p.lower().endswith(".crx"):
                eid, name, ver, _ = read_crx(p)
                self._query_store_async(eid, name, ver, p, local_hint=True)
            else:
                gid = guess_id(p)
                if not gid:
                    raise ValueError(self.t("err_unknown_file"))
                self._query_store_async(gid, "", "", p, local_hint=False)
        except Exception as e:
            self.log(self.t("log_failed", e), "warn")
            messagebox.showwarning(self.t("dlg_unrecognized"), str(e))

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
            messagebox.showwarning(self.t("dlg_unrecognized"), self.t("dlg_paste_hint"))
            return
        entry = self.ext_map.get(gid)
        name = entry["name"] if entry else ""
        ver = entry["info"].get("version", "") if entry else ""
        self._query_store_async(gid, name, ver, "text", local_hint=False)

    # -------------------------------------------------- 通道 A

    def _need(self):
        if not self.pending:
            messagebox.showinfo(self.t("dlg_need_title"), self.t("dlg_need_msg"))
            return None
        return self.pending

    def _check_channel_a_eligible(self, p):
        """通道 A 写入前校验：非商店扩展直接拦截提示，网络不可达时提醒确认"""
        label = p["name"] or p["id"]
        kind_key = p.get("kind_key")
        if kind_key == "kind_local":
            self.log(self.t("log_not_in_store"), "warn")
            messagebox.showwarning(
                self.t("dlg_store_missing_title"),
                self.t("dlg_store_missing_msg", label),
            )
            return False
        if kind_key == "kind_failed":
            return messagebox.askyesno(
                self.t("dlg_store_net_title"),
                self.t("dlg_store_net_msg", label),
            )
        return True

    def _after(self, summary_msg=None):
        running = chrome_running()
        if running:
            self.log(self.t("log_chrome_running"), "warn")
        else:
            self.log(self.t("log_apply_tip"))
        self.refresh()
        if summary_msg:
            body_key = "dlg_apply_ok_chrome_on" if running else "dlg_apply_ok_chrome_off"
            messagebox.showinfo(
                self.t("dlg_apply_ok_title"),
                self.t(body_key, summary_msg),
            )

    def do_force(self):
        p = self._need()
        if not p:
            return
        if not self._check_channel_a_eligible(p):
            return
        if not messagebox.askokcancel(
            self.t("dlg_confirm"),
            self.t("dlg_force_msg", p["name"] or p["id"], len(self.profiles)),
        ):
            return
        ids = list(_read_forcelist_entries(winreg.HKEY_CURRENT_USER).keys())
        if p["id"] not in ids:
            ids.append(p["id"])
        ops = [{"op": "write_forcelist", "ids": ids}]
        if p["id"] in self.extset:
            ops.append({"op": "del_extset", "id": p["id"]})
        if not self._run_policy_ops(ops):
            return
        msg = self.t("log_forced", p["name"] or p["id"])
        self.log(msg)
        self._after(msg)

    def do_normal(self):
        p = self._need()
        if not p:
            return
        if not self._check_channel_a_eligible(p):
            return
        ops = [{"op": "set_normal", "id": p["id"]}]
        cur_force = list(_read_forcelist_entries(winreg.HKEY_CURRENT_USER).keys())
        if p["id"] in cur_force:
            ops.append({"op": "write_forcelist", "ids": [i for i in cur_force if i != p["id"]]})
        if not self._run_policy_ops(ops):
            return
        msg = self.t("log_normal", p["name"] or p["id"])
        self.log(msg)
        self._after(msg)

    def do_remove(self):
        p = self._need()
        if not p:
            return
        eid = p["id"]
        label = p["name"] or eid
        cur_force = list(_read_forcelist_entries(winreg.HKEY_CURRENT_USER).keys())
        in_force = (eid in cur_force) or (eid in self.force)
        mode = self.extset.get(eid)

        # 若当前已在全局卸载策略中，点击可撤回该卸载策略
        if mode == "removed" and not in_force:
            if not messagebox.askokcancel(
                self.t("dlg_confirm"),
                self.t("dlg_remove_undo_msg", label),
            ):
                return
            if not self._run_policy_ops([{"op": "del_extset", "id": eid}]):
                return
            msg = self.t("log_removed_cancel", label)
            self.log(msg)
            self._after(msg)
            return

        ops = []
        if in_force or (mode and mode != "removed"):
            ans = messagebox.askyesnocancel(
                self.t("dlg_confirm"),
                self.t("dlg_remove_choice_msg", label),
            )
            if ans is None:
                return
            if eid in cur_force:
                ops.append({"op": "write_forcelist", "ids": [i for i in cur_force if i != eid]})
            if ans:
                ops.append({"op": "set_removed", "id": eid})
            else:
                ops.append({"op": "del_extset", "id": eid})
            if not self._run_policy_ops(ops):
                return
            if ans:
                msg = self.t("log_removed_all", label)
                self.log(msg)
            else:
                msg = self.t("log_removed", label)
                self.log(msg)
                self.log(self.t("log_removed_hint"))
            self._after(msg)
            return

        # 纯手动安装的扩展：写入 Chrome 'removed' 策略实现全配置卸载
        if not messagebox.askokcancel(
            self.t("dlg_confirm"),
            self.t("dlg_remove_manual_msg", label),
        ):
            return
        if not self._run_policy_ops([{"op": "set_removed", "id": eid}]):
            return
        msg = self.t("log_removed_all", label)
        self.log(msg)
        self._after(msg)

    def do_clear(self):
        if not messagebox.askokcancel(self.t("dlg_confirm"), self.t("dlg_clear_msg")):
            return
        if not self._run_policy_ops([{"op": "clear_all"}]):
            return
        msg = self.t("log_cleared")
        self.log(msg)
        self._after(msg)

    def do_kill_chrome(self):
        if not chrome_running():
            self.log(self.t("log_chrome_not_running"))
            self.render_status()
            return
        if not messagebox.askokcancel(self.t("dlg_confirm"), self.t("dlg_kill_chrome_msg")):
            return
        self.kill_chrome_btn.configure(state="disabled")

        def worker():
            err = None
            ok = False
            try:
                ok = kill_chrome_processes()
            except Exception as e:
                err = e

            def done():
                try:
                    self.kill_chrome_btn.configure(state="normal")
                except Exception:
                    pass
                if err is not None:
                    self.log(self.t("log_chrome_kill_err", err), "warn")
                elif ok:
                    self.log(self.t("log_chrome_killed"))
                else:
                    self.log(self.t("log_chrome_kill_left"), "warn")
                self.refresh()

            self.call_ui(done)

        threading.Thread(target=worker, daemon=True).start()

    def do_spread(self):
        idx = self.src_box.current()
        if idx < 0:
            return
        pdir, pname = self.profiles[idx]
        exts = read_extensions(pdir)
        ids = [
            e for e, i in exts.items()
            if i.get("location") in (1, 6, 7) and e not in BUILTIN_IDS
        ]
        if not ids:
            messagebox.showinfo(self.t("dlg_spread_none_title"), self.t("dlg_spread_none_msg"))
            return
        preview = "\n".join(exts[e]["name"] for e in ids[:10])
        msg = (
            self.t("dlg_spread_msg", len(ids), pname, preview)
            if self.lang == "en"
            else self.t("dlg_spread_msg", pname, len(ids), preview)
        )
        if not messagebox.askokcancel(self.t("dlg_confirm"), msg):
            return
        cur = list(_read_forcelist_entries(winreg.HKEY_CURRENT_USER).keys())
        ops = []
        for e in ids:
            if e not in cur:
                cur.append(e)
            if self.extset.get(e) in ("removed", "blocked"):
                ops.append({"op": "del_extset", "id": e})
        ops.insert(0, {"op": "write_forcelist", "ids": cur})
        if not self._run_policy_ops(ops):
            return
        self._persist_settings()
        summary = self.t("log_spread", len(ids))
        self.log(summary)
        self._after(summary)

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
                raise ValueError(self.t("dlg_unpack_need_file"))
            self.pool = [x for x in self.pool if x["id"] != p["id"]]
            self.pool.append({"id": p["id"], "name": p["name"] or p["id"][:8],
                              "version": p["version"] or ""})
            save_pool(self.pool)
            self.render_local()
            self.log(self.t("log_unpacked", p["id"]))
        except Exception as e:
            self.log(self.t("log_unpack_err", e), "warn")
            messagebox.showwarning(self.t("dlg_unpack_err_title"), str(e))
            return
        if self._run_policy_ops([{"op": "allow_local_sources"}]):
            self.log(self.t("log_allowed_src"))
            self.refresh()

    def do_open_pages(self):
        sel = self.local_lb.curselection()
        if not sel:
            messagebox.showinfo(self.t("dlg_need_title"), self.t("dlg_local_need_sel"))
            return
        p = self.pool[sel[0]]
        if not self._run_policy_ops([{"op": "allow_local_sources"}]):
            return
        where = self.ext_map.get(p["id"], {}).get("where", {})
        missing = [(d, n) for d, n in self.profiles if d not in where]
        if not missing:
            messagebox.showinfo(self.t("dlg_all_done_title"), self.t("dlg_all_done_msg"))
            return
        crx = os.path.join(PACK_DIR, p["id"] + ".crx")
        target = crx if os.path.exists(crx) else os.path.join(PACK_DIR, p["id"])
        self.log(self.t("log_missing_profs", len(missing), self.t("sep_list").join(n for _, n in missing)))
        self.log(self.t("log_drag_target", target))
        n = open_extension_pages(missing)
        if n == 1:
            messagebox.showinfo(self.t("dlg_opened_1_title"), self.t("dlg_opened_1_msg", target))
        else:
            messagebox.showinfo(self.t("dlg_opened_n_title", n), self.t("dlg_opened_n_msg", target))

    def do_local_del(self):
        sel = self.local_lb.curselection()
        if not sel:
            return
        p = self.pool[sel[0]]
        if not messagebox.askokcancel(
            self.t("dlg_confirm"),
            self.t("dlg_local_del_msg", p.get("name") or p["id"]),
        ):
            return
        self.pool = [x for x in self.pool if x["id"] != p["id"]]
        save_pool(self.pool)
        self.render_local()
        self.log(self.t("log_local_removed", p["id"]))


def _parse_lang_arg(argv):
    for a in argv:
        low = a.lower()
        if low.startswith("--lang="):
            val = low.split("=", 1)[1].strip()
            if val in ("auto", "zh", "en"):
                return val
        elif low in ("--en", "--english"):
            return "en"
        elif low in ("--zh", "--chinese"):
            return "zh"
    return None


def main():
    if _handle_apply_policy_cli(sys.argv[1:]):
        return

    args = {a.lower() for a in sys.argv[1:]}
    force_bg = "--background" in args or "--minimized" in args
    force_show = "--show" in args
    launched_by_autostart = "--autostart" in args
    lang_override = _parse_lang_arg(sys.argv[1:])

    hmutex = acquire_single_instance(wake_existing=not force_bg)
    if not hmutex:
        return

    mutex_ref = [hmutex]
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
            lang_override=lang_override,
            mutex_ref=mutex_ref,
        )
        try:
            root.mainloop()
        finally:
            app.quit_app()
    finally:
        if mutex_ref[0]:
            kernel32.CloseHandle(mutex_ref[0])


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
            messagebox.showerror(tr(_BOOT_LANG, "dlg_startup_err"), tb[-1500:])
        except Exception:
            pass
