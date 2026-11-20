# Peripheral Battery Tray · 外设电量

Windows 托盘小工具，用于查看受支持的 2.4 GHz 无线外设电量。原生版也支持能向 Windows 上报电量的经典蓝牙耳机。

| 版本 | 运行入口 | 文档 |
| --- | --- | --- |
| Python 版 | 配置 Python 环境后运行 `python/启动外设电量.cmd` | [Python 版说明](python/README.md) |
| C++ / Win32 原生版 | `native/dist/peripheral-battery-tray-native.exe` | [原生版说明](native/README.md) |

切换版本前，先从托盘退出当前版本。两个版本共用单实例锁，防止同时读取接收器。

本地构建产物放在各版本的 `dist/` 中，配置回退、日志和构建缓存各自保存在 `.data/` 中。
版本、设备支持和验证情况见 [项目状态](项目状态.md)。
