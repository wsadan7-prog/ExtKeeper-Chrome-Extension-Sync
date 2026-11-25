# ExtKeeper · Chrome 多配置扩展同步管家 (Windows Chrome Multi-Profile Extension Sync & Policy Manager)

<p align="center">
  <b>简体中文</b> | <a href="README_EN.md">English</a>
</p>

<p align="center">
  <b>免 Python 单文件 EXE 开箱即用 · HKCU 组策略跨配置同步 Chrome 扩展 · 中英文双语实时切换 · 开机自启 &amp; 默认后台系统托盘</b><br/>
  <sub>Sync &amp; batch-manage Chrome extensions across all local profiles on Windows via HKCU Group Policy — portable single-file EXE, bilingual UI (中文/EN), native tray, auto-start &amp; background auto-sync.</sub>
</p>

<p align="center">
  <a href="https://github.com/wsadan7-prog/ExtKeeper-Chrome-Extension-Sync/releases/latest"><b>⬇ 直接下载绿色单文件 ExtKeeper.exe（免装 Python）</b></a>
</p>

![ExtKeeper 主界面预览](docs/screenshot.png)

---

## 为什么需要它？

在一台 Windows 电脑上同时使用多个 Chrome 配置文件（工作账号、个人账号、开发测试环境等）时，Chrome 原生同步是**按 Google 账号隔离**的——在配置 A 安装的扩展不会出现在配置 B/C/D 中。

**ExtKeeper（扩展管家）** 利用 Windows 组策略（`HKCU\Software\Policies\Google\Chrome`）突破账号隔离，在不修改 `Secure Preferences` 的前提下，把商店扩展统一同步到本机所有 Chrome 配置文件；主界面以普通权限运行（保留 `.crx` 拖拽能力），仅在写入受保护的组策略键或清理 `HKLM` 冲突策略时按需触发 UAC 授权；并针对非商店本地 `.crx` 扩展提供解包、放行安装源与定向打开缺失配置扩展页的半自动流水线。

---

## 核心特性

- **双通道统一管理（Dual-Channel Management）**
  - **通道 A｜Chrome 商店扩展**：拖入 `.crx`、点击选文件、粘贴商店链接 / 32 位 ID，或单击左侧表格行，一键写入 `ExtensionInstallForcelist`（全配置强制同步）或 `ExtensionSettings`（`normal_installed` 自动安装且允许手动停用）；**「③ 一键删除」** 支持仅撤回托管策略或写入 `removed` 策略跨全部配置彻底卸载（含手动安装的扩展）。
  - **通道 B｜本地 / 私有扩展**：自动解析 CRX2 / CRX3 头部 ID，解包到 `packs\<ID>\` 并留存原包，自动写入 `ExtensionInstallSources` 放行 `file:///*`，一键仅给「还缺少该扩展」的配置文件批量打开 `chrome://extensions` 窗口。
- **账户名精准识别与三态可视化矩阵**
  - 优先读取 Google 账户名（`gaia_name`）并严格按 Chrome 自身配置顺序（`profiles_order`）排列，自动过滤已卸载扩展的空壳残骸记录。
  - 表格直观区分 **`● 已装`**、**`◐ 待开号生效`**、**`✕ 待卸载`**、**`○ 未装`**，操作写入成功后立刻给出清晰视觉反馈与生效指引。
- **按需 UAC 提权、冲突清理 & 一键关闭 Chrome 后台**
  - 主界面保持普通权限运行（不受 Windows UIPI 拖拽限制），仅在写入受保护组策略键时自动按需弹出 UAC 授权，也支持右下角一键「管理员模式重启」。
  - 自动检测并清理 `HKLM\Software\Policies\Google\Chrome` 下会覆盖 `HKCU` 的冲突旧扩展策略（绝不触碰 WebRTC / DNS 等其他浏览器策略）。
  - 内置 **「关闭 Chrome」** 按钮（顶部操作栏 & 托盘右键菜单）：先温和关闭窗口尽量保留标签页会话，再强制结束残留后台进程，彻底触发冷启动生效。
- **整套铺开 & 定时自动同步（One-Click Spread & Auto-Sync）**
  - 选定任意基准配置，一键将其全部商店扩展铺开到本机所有配置；
  - 支持开启 **「自动同步」**，每分钟轻量巡检基准配置新增的商店扩展并增量补齐策略（只增不删，不破坏已有手动设置）。
- **开机自启 & 默认后台系统托盘（Auto-Start & Background Tray）**
  - **开机自启**：写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ExtKeeper`，无黑框、免 UAC 提权（支持 `.exe` 与 `.pyw` 两种运行方式）。
  - **默认后台**：启动直接进入右下角系统托盘，点击关闭按钮（`×`）自动隐藏到托盘不退出；纯 Win32 API（`ctypes`）实现，零第三方托盘库依赖。
  - **单实例保护**：后台驻留时再次打开程序，自动唤醒已有主窗口至前台。
- **内置中英文双语界面 & 语言模式（Bilingual UI）**
  - 首次运行**自动跟随 Windows 系统语言**（中文系统默认简体中文，非中文系统默认 English）；
  - 点击主窗口右上角 **`EN` / `中文`** 按钮或托盘右键菜单，全界面（含按钮、表格、弹窗、托盘菜单与日志）**即时无感切换**并自动持久化记住偏好。
- **开箱即用 · 绿色便携**
  - 提供独立打包的免 Python 绿色单文件 **`ExtKeeper.exe`**（内置拖拽支持与图标），也支持单文件源码 `extkeeper.pyw` 直接运行。

---

## 快速开始

### 方式一：直接下载绿色单文件 EXE（推荐，免装 Python）

1. 前往 [Releases 最新版本页面](https://github.com/wsadan7-prog/ExtKeeper-Chrome-Extension-Sync/releases/latest) 下载 **`ExtKeeper.exe`**。
2. 放到任意固定文件夹（如 `D:\Tools\ExtKeeper\`）双击即可运行；配置文件 `settings.json`、本地扩展池 `local.json` 与 `packs\` 会自动保存在 `.exe` 同目录，绿色无残留。

### 方式二：从源码运行（Python 3.8+）

1. 确保本机已安装带 `tkinter` 的 Python 3.8+。
2. （可选）安装拖拽与图标增强依赖（不装也可正常运行）：
   ```bash
   pip install -r requirements.txt
   ```
3. 双击 **`启动.bat`**，或运行 `pythonw extkeeper.pyw`。

### 命令行参数（`.exe` 与 `启动.bat` 均支持）

| 参数 | 说明 |
| --- | --- |
| `--background` / `--minimized` | 强制以后台系统托盘方式静默启动 |
| `--show` | 强制前台显示主界面（忽略默认后台偏好） |
| `--lang=zh` / `--lang=en` / `--lang=auto` | 指定界面语言（简体中文 / English / 跟随系统） |
| `--autostart` | 开机自启内部标识（按已保存的「默认后台」设置决定是否静默） |

---

## 技术原理与 Chrome 版本限制说明

在 Chrome 137 ~ 158 实测确认以下平台限制：

1. `--load-extension` 启动参数在 Chrome 137 被移除，Chrome 142 连 `DisableLoadExtensionCommandLineSwitch` 回旋开关一并移除；
2. Chrome 官方策略规定：**非域控 Windows 机器（未加入 AD / Azure AD / Chrome Enterprise Core）禁止通过组策略强制安装 Chrome Web Store 以外的扩展**；
3. 直接篡改 `Secure Preferences` 的 HMAC 校验在 Chrome 升级时极易导致整个扩展列表判废，因此本工具绝不触碰该文件。

因此采用 **「商店扩展走 HKCU 组策略全自动同步（通道 A）+ 本地扩展解包放源并定向开页半自动安装（通道 B）」** 是非域个人 Windows 环境下最稳妥可靠的架构。

### 使用提示

- **必须彻底关闭所有 Chrome（含后台驻留）再重开**：Chrome 所有账户共用一个主进程，可直接点界面上的 **「关闭 Chrome」** 按钮彻底退出后再打开（或在 `chrome://policy` 点「重新加载政策」）。
- **需打开对应账户窗口才会触发生效**：Chrome 不会在后台替未打开过的账户下载扩展；重开 Chrome 后依次打开各个账户窗口，单个扩展约 **5–20 秒**自动完成下载，新装图标默认收在右上角 **🧩 拼图菜单**中，随后在本软件点「刷新」即可更新为 `●`。
- **强装与可停用模式区别**：强装模式（`force_installed`）下扩展页会提示「由贵单位管理」且无法单独停用；如需自动安装且允许自行开关，请使用 **「② 装，但允许我自己停用」**（注意：对曾手动卸载过该扩展的账户，Chrome 不会通过 `normal_installed` 自动重装，需先用 ① 强装）。
- **一键删除**：对托管扩展可选择仅撤回策略或写入 `removed` 策略彻底卸载全部配置（含手动安装版本，卸载后仍允许未来手动重装）。

---

## 仓库结构

```text
.
├── extkeeper.pyw         # 主程序（双通道策略同步、中英双语界面、开机自启、原生 Win32 托盘）
├── 启动.bat              # Windows 源码双击静默启动脚本（支持透传命令行参数）
├── extkeeper.ico         # 应用窗口与系统托盘图标
├── requirements.txt      # 源码运行可选依赖（tkinterdnd2、pillow）
├── README.md             # 简体中文说明文档
├── README_EN.md          # English Documentation
├── 读我.txt              # 简明纯文本本地使用指南
├── LICENSE               # MIT License
└── docs/
    ├── screenshot.png    # 中文界面预览图（脱敏演示数据）
    └── screenshot_en.png # 英文界面预览图（脱敏演示数据）
```

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
