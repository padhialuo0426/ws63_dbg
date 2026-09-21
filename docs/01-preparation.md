# 01 硬件接线与主机准备

本篇帮助首次使用 BearPi-Pico H3863 的开发者完成接线、探针权限和主机工具准备。
教程以 v1.0.102 SDK 的 blinky 应用为基线；完成本篇后，再修改固件以开启 SWD。

**本篇目录**

- [1.1 设置工程与 SDK 路径](#11-设置工程与-sdk-路径)
- [1.2 连接开发板与探针](#12-连接开发板与探针)
- [1.3 检查探针与访问权限](#13-检查探针与访问权限)
- [1.4 准备构建与烧录工具](#14-准备构建与烧录工具)

## 1.1 设置工程与 SDK 路径

在终端中设置路径，按实际存放位置修改；后续命令使用这两个变量：

```bash
export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
export WS63_DEBUG="填入实际的 ws63_dbg 路径"
```

把引号中的说明替换为本机两个目录的绝对路径，保留引号；新开终端时重新设置。
根目录变量用于跨目录访问，不要求 SDK 与调试器放在同一父目录下。
文档中的工程文件和输出文件使用相对于工作目录的路径；先执行示例中的 `cd`，再运行后续命令。
`/dev/...` 是 Linux 设备路径，`/workspace` 是部分 ELF 记录的编译时源码前缀，两者保留原义。

## 1.2 连接开发板与探针

按下表连接调试器和开发板，并确认两者共地。

| 调试器 | H3863 |
|---|---|
| SWDIO | GPIO_13 |
| SWCLK | GPIO_14 |
| GND | GND |
| VTref（如果有） | 3.3V（只作电平参考，不要用调试器给板子供电） |

板子用自己的 Type-C 口供电。**不需要接 nRESET**：复位通过调试模块完成（见 [复位行为](04-debugger.md#46-复位行为)）。

## 1.3 检查探针与访问权限

先确认探针提供的接口类型，再检查当前用户是否有设备访问权限。

| 类型                     | 状态                 | 读 Flash 速度  |
| ---------------------- | ------------------ | ----------- |
| CMSIS-DAP v1（USB HID）  | 已验证基本调试、读观察点、复位和读回 | 以 dump 输出为准 |
| CMSIS-DAP v2（USB bulk） | 已实现，**尚未真机验证**     | —           |

- v1 直接读写 Linux 的 `/dev/hidraw*`，v2 通过 ctypes 调用系统自带的 libusb-1.0，都不需要安装第三方 Python 库。
  一个调试器同时提供两种接口时优先用 v2。
- 当前用户需要有设备的访问权限（udev 规则；装了 openocd 一般会带上 `60-openocd.rules`）。
- 同时插了多个 CMSIS-DAP 时，用 `--serial <序列号>` 指定；切换到工程根目录后列出序列号：

  ```bash
  cd "$WS63_DEBUG"
  python3 cmsisdap.py
  ```

## 1.4 准备构建与烧录工具

调试和烧录需要 Python 3、[ws63flash](https://github.com/goodspeed34/ws63flash)，
以及 SDK 内附的 `riscv32-linux-musl-gdb`。不要使用仅支持主机架构的普通 GDB。
SDK 编译环境搭建与编译方法参考 [小熊派官方开发文档](https://www.bearpi.cn/core_board/bearpi/pico/h3863/)。

```bash
python3 --version
ws63flash --version
ls -l /dev/serial/by-id/
```

烧录口选择板载 CH340（例如 `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`），
不要误选 DAPLink 的虚拟串口。运行 GDB、dump 和硬件测试时必须独占探针。

---

[返回总目录](../README.md#教程目录) · 下一篇：[02 在 SDK 中开启 WS63 调试接口](02-enable-swd.md)
