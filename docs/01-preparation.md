# 01 硬件接线与主机准备

本篇介绍调试宿主机环境、开发板接线和所需工具。完成准备后，按 [第 02 篇](02-enable-swd.md)
在固件中开启 SWD 调试接口。教程使用 BearPi-Pico H3863 和 SDK v1.0.102 的 blinky 示例。

**本篇目录**

- [1.1 调试宿主机环境](#11-调试宿主机环境)
- [1.2 设置工程与 SDK 路径](#12-设置工程与-sdk-路径)
- [1.3 连接开发板与探针](#13-连接开发板与探针)
- [1.4 检查探针与访问权限](#14-检查探针与访问权限)
- [1.5 准备构建与烧录工具](#15-准备构建与烧录工具)

## 1.1 调试宿主机环境

本教程的调试操作在以下 Linux 宿主机上验证：

| 项目 | 环境 |
|---|---|
| 操作系统 | Arch Linux（滚动发行版） |
| 内核 | Linux `7.2.6-arch2-1` |
| 架构 | `x86_64` |

这些信息用于复现调试环境，不要求使用相同的发行版或内核版本。
SDK 的编译环境按 [小熊派官方文档](https://www.bearpi.cn/core_board/bearpi/pico/h3863/) 准备。

## 1.2 设置工程与 SDK 路径

在每个使用教程命令的终端中设置以下变量，将引号中的说明替换为实际目录的绝对路径：

```bash
export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
export WS63_DEBUG="填入实际的 ws63_dbg 路径"
```

后续命令通过这两个变量访问 SDK 和调试器工程。其余工程路径均相对于命令执行目录，
因此先执行示例中的 `cd`，再运行后续命令。
`/dev/...` 表示 Linux 设备路径；`/workspace` 用于匹配 ELF 中的编译时源码路径，不需要在本机创建。

## 1.3 连接开发板与探针

按下表连接 CMSIS-DAP 探针与开发板：

| 探针 | H3863 |
|---|---|
| SWDIO | GPIO_13 |
| SWCLK | GPIO_14 |
| GND | GND |
| VTref（如果有） | 3.3V，仅作目标电压参考 |

开发板通过自身的 Type-C 接口供电，不使用探针的电源输出供电。
本教程不连接 nRESET，复位由调试模块完成，具体行为见 [复位说明](04-debugger.md#46-复位行为)。

## 1.4 检查探针与访问权限

调试器支持以下两种 CMSIS-DAP 接口。同一探针同时提供两种接口时，工具优先使用 v2。

| 接口 | Linux 访问方式 | 依赖 |
|---|---|---|
| CMSIS-DAP v1 HID | `/dev/hidraw*` | Python 标准库 |
| CMSIS-DAP v2 bulk | USB bulk 端点 | Python 标准库和系统 libusb-1.0 |

在工程根目录枚举探针：

```bash
cd "$WS63_DEBUG"
python3 cmsisdap.py
```

输出列出接口类型、产品名和序列号。连接多个探针时，为后续命令添加 `--serial` 并指定所需序列号。
若未找到探针，检查 USB 连接及设备权限。当前用户需要访问探针对应的 USB 或 hidraw 设备，
可通过发行版提供的调试器 udev 规则配置权限。

工具通过 `DAP_Info` 自动读取命令包长和缓冲包数，已验证 64 字节和 512 字节命令包。
无需按 USB 端点大小手动修改包长；传输检查方法见 [验证内存导出](10-verify.md#104-验证内存导出)。

## 1.5 准备构建与烧录工具

基础调试需要 Python 3；LiteOS 任务检查和现场分析另需 pyelftools，安装步骤见 [准备调试 ELF](07-rtos.md#71-准备调试-elf)。
首次串口烧录使用 [ws63flash](https://github.com/goodspeed34/ws63flash)，源码调试使用 SDK 内附的 `riscv32-linux-musl-gdb`。

检查 Python、串口烧录工具和设备路径：

```bash
python3 --version
ws63flash --version
ls -l /dev/serial/by-id/
```

烧录口选择板载 CH340 对应的设备，例如 `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`。
按实际枚举结果替换后续烧录命令中的路径，避免选到 DAPLink 的虚拟串口。
GDB 服务端、内存导出和硬件测试必须依次运行，不能同时占用同一探针。

---

[返回总目录](../README.md#教程目录) · 下一篇：[02 在 SDK 中开启 WS63 调试接口](02-enable-swd.md)
