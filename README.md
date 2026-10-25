# 划词翻译（ScreenTranslate）

选中文字，立刻在光标旁看到译文。Windows 与 Linux 双平台，默认走免费翻译接口，**不需要任何 API Key**。

- **Windows**：拖动选中 / 双击选词即翻译（全局鼠标钩子，WPS、Word、浏览器、PDF 通用）
- **Linux（Ubuntu）**：监听 PRIMARY 选区，任意程序里选中文字即翻译；也可把 `--once` 绑成快捷键
- 支持 Google 免费接口、MyMemory 免费接口、DeepL、OpenAI 兼容接口
- 单文件可执行程序，无需安装 Python；配置使用 JSON，界面可改

## 平台支持

| 平台 | 状态 | 划词方式 | 说明 |
| --- | --- | --- | --- |
| Windows 10 / 11 (x64) | 支持 | 低层鼠标钩子 + 模拟 Ctrl+C | 推荐使用 |
| Ubuntu 22.04+ (X11) | 支持 | PRIMARY 选区轮询 + xclip | 推荐使用 |
| Ubuntu (Wayland) | 可用 | PRIMARY 选区轮询 + wl-paste | 弹窗位置可能不精确，建议配合快捷键 |

## 下载与安装

### 方式一：直接下载可执行文件

在 [Releases](../../releases) 页面下载对应平台的文件：

- `ScreenTranslate-windows-x64.exe` —— 双击即用
- `ScreenTranslate-linux-x64` —— `chmod +x` 后运行

### 方式二：从源码运行

```bash
git clone https://github.com/<your-name>/screen-translate.git
cd screen-translate

# Windows
python windows/screen_translate.py

# Ubuntu
sudo apt install python3 python3-tk xclip wl-clipboard
python3 linux/screen_translate_linux.py
```

## 使用

### Windows

| 操作 | 效果 |
| --- | --- |
| 拖选文字 | 松手后光标旁弹出译文 |
| 双击选词 | 翻译被选中的词 |
| `Alt+F1` | 暂停 / 继续划词 |
| `F8` | 手动翻译当前选中内容 |
| `Esc` | 关闭弹窗 |

### Ubuntu

选中文字即自动翻译（需要在设置里保持"自动监听选区"开启）。也可以把命令行绑成桌面快捷键：

```bash
# 单次翻译当前选区并弹窗
ScreenTranslate --once

# 只把译文打印到终端（适合写脚本）
ScreenTranslate --print
```

GNOME 绑定快捷键：`设置 → 键盘 → 查看及自定义快捷键 → 自定义快捷键`，命令填 `ScreenTranslate --once`，按键设为 `Alt+F1`。

## 配置

图形界面里改完点"保存设置"即可。配置文件位置：

- Windows：可执行文件同目录 `screen_translate.json`
- Linux：`~/.config/screen-translate/config.json`

| 字段 | 说明 |
| --- | --- |
| `provider` | `auto`（默认，Google→MyMemory 自动降级）/ `google` / `mymemory` / `deepl` / `openai` |
| `target_lang` | `auto`（中文→英文，其它→中文）或 `zh-CN` / `en` / `ja` / `ko` |
| `deepl_api_key` `openai_api_key` | 仅在选择对应通道时需要 |
| `trigger_drag` `trigger_double_click` | Windows：是否用拖选 / 双击触发 |
| `watch_selection` `poll_interval_ms` | Linux：是否自动监听选区、轮询间隔 |
| `popup_timeout_sec` | 弹窗自动关闭时间 |
| `max_text_len` | 单次翻译最大字符数 |
| `autostart` | 开机自启（Windows 写注册表 Run，Linux 写 `~/.config/autostart`） |

## 自行打包

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
# 产物：dist\ScreenTranslate.exe
```

### Ubuntu

```bash
bash linux/build.sh
# 产物：dist/ScreenTranslate
```

> PyInstaller 只能产出**当前系统**的可执行文件，所以 Linux 版必须在 Linux 上构建。

### GitHub Actions 自动构建

仓库内置 [.github/workflows/build.yml](.github/workflows/build.yml)：

- 每次 push / PR：自动构建 Windows 与 Linux 产物，上传为 Actions Artifact
- 打 `v*` 标签（如 `v1.0.0`）：自动创建 Release 并附上两个平台的二进制

```bash
git tag v1.0.0
git push origin v1.0.0
```

## 实现原理

**Windows**

1. `SetWindowsHookEx(WH_MOUSE_LL)` 监听全局鼠标，识别"按下 → 移动 → 抬起"的拖选，或双击选词
2. 用 `SendInput` 发送 `Ctrl+C`，读取剪贴板拿到选中文本，随后还原原剪贴板
3. 调用翻译接口，用无边框置顶窗口在光标旁显示原文与译文

**Linux**

1. 轮询 X11 PRIMARY selection（`xclip -selection primary -o`，Wayland 用 `wl-paste --primary`）
2. 内容变化且非空 → 触发翻译
3. 同样的置顶弹窗展示结果；`--once` / `--print` 供快捷键与脚本使用

## 已知限制

- **没有 OCR**：图片型 PDF、扫描件、游戏画面里"选不中"的文字无法翻译
- **Wayland 限制**：无法获取全局鼠标坐标与全局热键，弹窗位置可能贴在指针附近但不精确；快捷键需要在桌面环境里绑定
- **Linux 需要 xclip 或 wl-clipboard**：否则无法读取选区，程序会在界面顶部直接提示安装命令
- **免费接口是非官方端点**：Google 的 `translate_a/single` 与 MyMemory 的公开接口都没有 SLA，可能限速或随时变更；需要稳定请配置 DeepL / OpenAI
- **Windows 下 Alt+F1 可能与欧路词典冲突**：注册失败会在日志中提示，改用界面按钮即可

## 免责声明

本项目仅用于个人学习与效率提升。翻译请求发往各服务商公开的免费接口，请遵守对应服务的使用条款；请勿用于商业批量调用或任何违反当地法律法规的用途。

## 许可证

[MIT](LICENSE)
