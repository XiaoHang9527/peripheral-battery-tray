# 外设电量原生版

Windows 10 / 11 x64。从仓库根目录运行 `native/dist/peripheral-battery-tray-native.exe`，再从系统托盘打开「概览」或「设置」。
可将可执行文件放在要长期使用的目录，再开启「开机自启」。Python 版和原生版共用互斥锁，
切换版本时先从托盘退出正在运行的版本，避免两版同时读取接收器。

原生版使用 Windows HID、SetupAPI、Win32、GDI+、WinHTTP 和 CNG。可执行文件使用静态 C++ 运行库，
运行时无需另装 Python、Tkinter、Pillow、pystray 或 .NET。初次启动和读取电量可以离线完成。

## 功能

- 支持指定的 2.4 GHz 接收器和能向 Windows 上报电量的经典蓝牙耳机。见 [支持的品牌、型号和连接方式](../README.md#支持的设备)。
- Logitech HID++ 会读取设备型号或类型，并复核首次电量。Rapoo 充电时如果接收器只上报充电状态，电量会显示为未知。
- 托盘状态、独立任务栏圆环、设备重命名、热插拔识别、低电量提醒和开机自启。
- 在托盘图标的右键菜单或「概览 → 更多操作」中开关「任务栏圆环」。「设置」中可以为所选设备恢复圆环。找不到托盘图标时，再次双击 EXE 可打开概览。
- 点击概览中设备行右侧的「⋯」，或右键点击设备，可重命名或单独开关该设备的圆环。
- 悬停在任务栏圆环上可查看设备名称、上次电量、状态和读取时间。左键立即刷新，右键打开设备概览。
- 自动刷新只读取近期有活动的外设。设备活动恢复后可提前刷新，持续使用时按「设置」中的间隔更新，默认 60 秒。休眠或悬停时保留已读电量。检测不到设备活动时，可用「立即刷新」读取。
- Logitech Unifying（优联）从 USB 接收器读取各槽位的活动计数。其他受支持的 2.4G 设备通过 Windows 的输入设备标识匹配活动。经典蓝牙耳机显示 Windows 已上报的电量。
- 「设置 → 高级日志设置」可调整单份日志大小、历史日志份数及保留天数。默认每份 512 KB，保留一份历史日志。
- 可即时预览跟随 Windows、浅色和深色外观。点击「保存」后生效。
- 设置和运行日志默认放在 `%LOCALAPPDATA%\PeripheralBatteryTrayNative`。该目录不可写时，使用程序旁的 `.data/native`。在程序旁建立空文件 `portable.mode` 可主动使用随身设置。
- 「更多操作 → 检查更新」查询该仓库的原生版发布。用户确认后才下载，程序验证文件大小和 GitHub 提供的 SHA-256，再退出、替换并重新启动。成功更新后，程序目录中的 `.pbt-native-previous.exe.bak` 保存上一个版本。

## 雷柏接收器匹配

部分型号在两版中匹配不同的 USB 接收器。下表中的长度指接收器的 HID 输入报文长度，单位为字节。

| 型号 | 原生版 USB ID / 长度 | Python 版 USB ID / 长度 |
| --- | --- | --- |
| VT3 Pro | `24AE:1215` / 7 | `24AE:1215` / 7 |
| VT1 Pro Max | `24AE:1233` / 7 | `24AE:1233` / 7 |
| VT0 | `24AE:1218` / 13、16 或 19 | `24AE:1410` / 13、16 或 19 |
| VT9（第二代） | `24AE:1243` / 16 | `24AE:1414` / 13、16 或 19 |
| VT3 Max（第二代） | `24AE:124D` / 16 | `24AE:1417` / 13、16 或 19 |

## 构建和测试

使用 LLVM-MinGW 或配置为 Windows x64 目标的 MinGW-w64 C++20 工具链及配套 `windres`。
在仓库根目录用 PowerShell 构建并运行协议测试：

```powershell
.\native\build.ps1 -Compiler 'C:\path\to\x86_64-w64-mingw32-clang++.exe' -Test
```

构建结果位于 `native/dist/peripheral-battery-tray-native.exe`。
不启动托盘的实机检查可用 `peripheral-battery-tray-native.exe --probe probe.tsv`。
检查会查询接收器，需先退出正在运行的外设电量实例。

发布原生版时修改 `update.hpp` 中的版本号，使用独立标签 `native-vX.Y.Z`。
GitHub Actions 在两版测试通过后上传 `peripheral-battery-tray-native.exe` 和 `PicoJSON-LICENSE.txt`。
原生版为 GitHub 仓库的最新正式发布。Python 版使用独立的 `vX.Y.Z` 标签和专用更新包。

GitHub 发布信息由内置的 [PicoJSON 1.3.0](https://github.com/kazuho/picojson/tree/v1.3.0) 解析，
许可证和版权声明位于 `third_party/picojson.h`。
