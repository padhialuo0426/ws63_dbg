# 02 在 SDK 中开启 WS63 调试接口

本指南说明 SWD 初始化代码的具体位置，以及修改后的构建和烧录顺序。
先按 [硬件接线与主机准备](01-preparation.md) 连接 GPIO_13、GPIO_14 和 GND。
适用 SDK 为 BearPi-Pico H3863 v1.0.102，基线提交 `08d2880956249642cc90d7eae9b80ed721646967`。

**本篇目录**

- [2.1 确认前提与工程路径](#21-确认前提与工程路径)
- [2.2 在 flashboot 中添加 SWD 初始化](#22-在-flashboot-中添加-swd-初始化)
- [2.3 依次构建 flashboot 与应用](#23-依次构建-flashboot-与应用)
- [2.4 烧录并复位](#24-烧录并复位)
- [2.5 备选：仅在应用里开启 SWD](#25-备选仅在应用里开启-swd)
- [2.6 下一步](#26-下一步)

## 2.1 确认前提与工程路径

在终端中设置路径，按实际存放位置修改：

```bash
export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
export WS63_DEBUG="填入实际的 ws63_dbg 路径"
```

**固件必须先打开 SWD。** WS63 上电后 GPIO_13/14 是普通 GPIO，调试口是关着的，必须由固件把它们切成
SWD 功能（复用模式 4）。SDK 自带的代码不会做这件事，所以默认固件是连不上的
（现象：读 DPIDR 没有应答，报 `no SWD response`）。

## 2.2 在 flashboot 中添加 SWD 初始化

打开 SDK 根目录下的 `bootloader/flashboot_ws63/startup/main.c`，找到 `start_fastboot()`。
在函数里的 `fix_io_level();` **之后**、原有的 `uapi_reg_setbits(REG_CMU_CFG0, ...)` **之前**，
加入下面三条初始化调用。如果调用已经存在，不要重复添加。

该版本文件已包含 `#include "pinctrl.h"`，因此不需要新增头文件。移植到其他 SDK 版本时，
先检查文件路径、函数和头文件是否一致，不要仅按行号插入。

插入位置的上下文如下；保留该函数中其余代码：

```c
    fix_io_level();

    /* Enable SWD: GPIO_13 = SWDIO, GPIO_14 = SWCLK. */
    uapi_pin_init();
    uapi_pin_set_mode(GPIO_13, PIN_MODE_4);
    uapi_pin_set_mode(GPIO_14, PIN_MODE_4);

    uapi_reg_setbits(REG_CMU_CFG0, 3, 3, 0x7);
```

`uapi_pin_init()` 注册引脚驱动，不会修改引脚复用。随后两次 `uapi_pin_set_mode()` 才把
GPIO_13/14 切换为 SWD。未初始化驱动时，设置复用会返回 `ERRCODE_PIN_NOT_INIT`。

flashboot 是此 SDK 中最早可修改的启动代码，位于 BootROM 和 SSB 之后。
在这里打开 SWD，可以在应用 `main()` 设置断点；不必修改应用本身。
本教程直接说明修改位置，仓库不分发 `.patch` 文件。

> 这些调用会让调试口从 flashboot 开始保持开放。开发结束后，可移除这三次调用，
> 或改用项目自己的编译开关控制；移除后仍需重新构建 flashboot 并烧录。

## 2.3 依次构建 flashboot 与应用

SDK 编译环境搭建与具体编译方法参考 [小熊派官方开发文档](https://www.bearpi.cn/core_board/bearpi/pico/h3863/)。

完成环境配置后，在 SDK 根目录选择应用示例，再依次编译 flashboot 和应用：

1. 打开应用配置，在 `Application` 中选择 blinky 示例，退出并保存。已经配置好示例时可跳过此步。

   ```bash
   cd "$WS63_SDK"
   ./build.py menuconfig ws63-liteos-app
   ```

2. 编译 flashboot，生成包含 SWD 初始化代码的引导镜像。

   ```bash
   ./build.py ws63-flashboot
   ```

3. 编译应用并将新的 flashboot 打入固件包。

   ```bash
   ./build.py ws63-liteos-app
   ```

单独编译 `ws63-liteos-app` 不会重新编译 flashboot。
预期分别出现 `Build target:ws63_flashboot success` 和 `packet success!`。
编译完成后，在 SDK 根目录验证固件包包含刚构建的主、备 flashboot：

```bash
cd "$WS63_SDK"
python3 - <<'PYCODE'
from pathlib import Path
output = Path('output/ws63')
package = (output / 'fwpkg/ws63-liteos-app/ws63-liteos-app_all.fwpkg').read_bytes()
for name in ('flashboot_sign.bin', 'flashboot_backup_sign.bin'):
    boot = (output / 'acore/ws63-flashboot' / name).read_bytes()
    assert boot in package, name + ' is missing from package'
    print(name, len(boot), 'bytes: packaged')
PYCODE
```

本基线两份镜像均为 50304 字节。镜像大小会随代码变化，不能单独作为 SWD 修改生效的证据。

## 2.4 烧录并复位

关闭占用 CH340 的串口工具后执行下面的命令。在出现 `Waiting for device reset...` 时按板上的复位键：

```bash
cd "$WS63_SDK"
ws63flash -b 115200 --flash /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 \
    output/ws63/fwpkg/ws63-liteos-app/ws63-liteos-app_all.fwpkg
```

输出 `Done. Reseting device...` 且退出码为 0 后，再运行连接检查。
如果等待复位超时，重新运行命令并及时按键；避免无限循环重试掩盖其他错误。

本教程使用 `-b 115200` 烧录；更高波特率需另行验证。
flashboot 无法启动时，可通过 BootROM 串口下载重新烧录，但前提是供电、复位和下载口仍可用。

## 2.5 备选：仅在应用里开启 SWD

不修改 flashboot 时，可在引脚驱动已经初始化的应用入口调用
`uapi_pin_set_mode(GPIO_13, PIN_MODE_4)` 和 `uapi_pin_set_mode(GPIO_14, PIN_MODE_4)`。
调试连接只能在这两次调用之后建立，不能保证命中应用 `main()`；本文的复位到 `main()` 流程要求在 flashboot 中开启 SWD。

## 2.6 下一步

- [检查连接并使用 GDB](03-getting-started.md#31-快速开始连接已开启-swd-的板卡)，验证修改后固件的调试接口。
- [查阅复位行为](04-debugger.md#46-复位行为)，区分 ndmreset 和整片复位。

---

上一篇：[01 硬件接线与主机准备](01-preparation.md) · [返回总目录](../README.md#教程目录) · 下一篇：[03 连接 GDB 与基础调试](03-getting-started.md)
