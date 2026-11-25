# ExtKeeper — Multi-Profile Chrome Extension Sync & Policy Manager for Windows

<p align="center">
  <a href="README.md">简体中文</a> | <b>English</b>
</p>

<p align="center">
  <b>Sync and batch-manage Chrome extensions across all local profiles on Windows via HKCU Group Policy — no admin privileges required, with native system tray, launch-at-startup &amp; background auto-sync.</b>
</p>

![ExtKeeper UI Preview](docs/screenshot.png)

---

## Why ExtKeeper?

When running multiple Chrome profiles on the same Windows machine (work, personal, dev, testing), Chrome's built-in sync is strictly **isolated per Google account** — installing an extension in Profile A never propagates it to Profiles B, C, or D.

**ExtKeeper** solves this cleanly using per-user Windows Group Policy (`HKCU\Software\Policies\Google\Chrome`):
- **No administrator / UAC elevation required** (HKCU registry only).
- **Never tampers with `Secure Preferences`** or Chrome's HMAC signatures (zero risk of corrupting your profile).
- Covers both **Chrome Web Store extensions** (fully automated sync & removal) and **unpacked / private `.crx` extensions** (streamlined semi-automated installation).

---

## Key Features

- **Dual-Channel Extension Management**
  - **Channel A — Chrome Web Store Extensions**: Drop a `.crx`, pick a file, paste a Web Store URL / 32-char ID, or click any row in the matrix to apply `ExtensionInstallForcelist` (force-install across all profiles) or `ExtensionSettings` (`normal_installed`, auto-installed yet user-toggleable), or remove from policy in one click.
  - **Channel B — Local / Private Extensions**: Parses CRX2/CRX3 protobuf headers to extract extension IDs, unpacks to `packs\<ID>\`, enables `ExtensionInstallSources` (`file:///*`), and opens `chrome://extensions` **only for profiles still missing that extension**.
- **Baseline Spread & Background Auto-Sync**
  - Pick any baseline profile (e.g. `Default`) and spread all of its Web Store extensions to every local profile in one click.
  - Enable **Auto-Sync** (`自动同步`) to periodically scan the baseline profile every 60 seconds and incrementally add newly installed store extensions to policy (additive only — never deletes existing manual policies).
- **Launch at Startup & Start in Background (Zero-Dependency Native Tray)**
  - **Auto-Start (`开机自启`)**: Registers `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ExtKeeper` using `pythonw.exe --autostart` for silent, console-free startup without admin rights.
  - **Start in Background (`默认后台`)**: Launches directly into the Windows system tray; clicking the window close button (`×`) hides to tray instead of exiting. Built directly on Win32 APIs via `ctypes` (no `pystray` or `pywin32` required).
  - **Single-Instance Activation**: Launching ExtKeeper again while it runs in the tray automatically brings the existing window to the foreground.
- **Single-File & Zero Hard Dependencies**
  - Everything lives in [extkeeper.pyw](extkeeper.pyw) using only the Python standard library (`tkinter`, `ctypes`, `winreg`, etc.). Drag-and-drop (`tkinterdnd2`) and runtime icon generation (`pillow`) are completely optional.

---

## Quick Start

### Requirements

- Windows 10 / 11
- Python 3.8+ (with `tkinter` included)

### Optional Dependencies (Recommended)

ExtKeeper runs out of the box without third-party packages (clicking the drop zone opens a standard file picker). To enable drag-and-drop and dynamic multi-size icon generation:

```bash
pip install -r requirements.txt
```

### Launching

1. Double-click **`启动.bat`** (automatically locates `pythonw.exe` or `pyw.exe` and starts without a console window).
2. Or launch via CLI:

```bash
pythonw extkeeper.pyw
```

Supported CLI flags (also forwarded by `启动.bat`):

| Flag | Behavior |
| --- | --- |
| `--background` / `--minimized` | Force silent launch into the Windows system tray |
| `--show` | Force showing the main window on startup |
| `--autostart` | Startup launcher flag (respects saved background preference) |

---

## Technical Background & Chrome Limitations

Verified on modern Chrome builds (Chrome 137–158):

1. `--load-extension` was removed in Chrome 137, and its fallback policy `DisableLoadExtensionCommandLineSwitch` was removed in Chrome 142.
2. Per official Chrome Enterprise documentation, **off-store extensions cannot be force-installed via policy on non-domain-joined Windows machines** (outside AD / Azure AD / Chrome Enterprise Core).
3. Modifying `Secure Preferences` directly breaks HMAC validation across Chrome updates and risks wiping the profile's extension list.

Splitting Store extensions (Channel A via HKCU Policy) from Private/Local `.crx` packages (Channel B via `ExtensionInstallSources` + targeted profile windows) is therefore the cleanest, safest architecture on personal Windows machines.

### Good to Know

- After updating policies, **fully exit Chrome (including its background tray icon)** and reopen it for changes to take effect.
- Extensions added via **Force Install** show *"Managed by your organization"* and cannot be disabled individually; use **Normal Install (allow disable)** if you prefer toggleable auto-installs.
- **One-Click Remove** unregisters the extension from policy so Chrome automatically uninstalls policy-managed copies on restart (previously manually installed copies may remain).

---

## Repository Structure

```text
.
├── extkeeper.pyw       # Main application (UI, dual-channel sync, auto-start, native Win32 tray)
├── 启动.bat            # Windows launcher script (console-free pythonw/pyw lookup + arg forwarding)
├── extkeeper.ico       # Application window & system tray icon
├── requirements.txt    # Optional dependencies (tkinterdnd2, pillow)
├── README.md           # Chinese documentation
├── README_EN.md        # English documentation
├── 读我.txt            # Plain-text quick reference (Chinese)
├── LICENSE             # MIT License
└── docs/
    └── screenshot.png  # Anonymized UI preview screenshot
```

---

## License

Released under the [MIT License](LICENSE).
