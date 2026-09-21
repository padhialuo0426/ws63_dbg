# 03 连接 GDB 与基础调试

本篇带你连接 GDB、调试 blinky 并导出内存。开始前先完成 [硬件与主机准备](01-preparation.md)，
再按 [开启调试接口](02-enable-swd.md) 修改、构建并烧录固件；已开启 SWD 的板卡可直接从本篇开始。
以下示例使用 BearPi-Pico H3863 v1.0.102 SDK 的 blinky 应用。

**本篇目录**

- [3.1 快速开始：连接已开启 SWD 的板卡](#31-快速开始连接已开启-swd-的板卡)
- [3.2 检查连接并使用 GDB](#32-检查连接并使用-gdb)
  - [3.2.1 调试 blinky 与复位](#321-调试-blinky-与复位)
- [3.3 读回 ROM、Flash 和 RAM](#33-读回-romflash-和-ram)
- [3.4 排查连接和调试失败](#34-排查连接和调试失败)
- [3.5 下一步](#35-下一步)

## 3.1 快速开始：连接已开启 SWD 的板卡

目标板已开启 SWD 后，按以下顺序检查连接并启动 GDB。

1. 在终端中设置路径并检查连接：

   ```bash
   export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
   export WS63_DEBUG="填入实际的 ws63_dbg 路径"
   cd "$WS63_DEBUG"
   python3 cmsisdap.py
   python3 ws63dbg.py
   ```

   正常输出包含 `DPIDR 5ba02477`。

2. 在同一终端启动 GDB 服务端：

   ```bash
   python3 gdbserver.py
   ```

   等待 `listening on 127.0.0.1:3333`。此时只建立 SWD 连接，GDB 接入后才暂停 CPU。

3. 在另一终端设置路径，从 SDK 根目录启动其自带的 RISC-V GDB：

   ```bash
   export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
   export WS63_DEBUG="填入实际的 ws63_dbg 路径"
   cd "$WS63_SDK"
   tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb \
       -x "$WS63_DEBUG/ws63.gdbinit" \
       output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf
   ```

4. 在 GDB 设置 blinky 断点并继续：

   ```gdb
   break blinky_cmsis.c:29
   continue
   bt 2
   ```

   正常情况下停在 `uapi_gpio_toggle(CONFIG_BLINKY_PIN)`。结束时执行 `detach`、`quit`，
   再回到服务端终端按 Ctrl-C 释放探针。

## 3.2 检查连接并使用 GDB

按快速开始连接后，用本节检查状态和源码映射。

正常连接输出示例：

```text
probe    CMSIS-DAP v2 (DAPLink-HS CMSIS-DAP)
DPIDR    5ba02477
dmstatus 00000c82
```

探针名称和 `dmstatus` 随设备及 CPU 状态变化；WS63 的 DPIDR 应为 `5ba02477`。

`ws63.gdbinit` 启用自动硬件断点，连接 `:3333`，并定义刷新寄存器缓存的 `reset` 命令。
如果 ELF 中记录的编译时源码路径与本地 SDK 路径不同，按实际路径调整脚本中的 `set substitute-path`。
其中 `/workspace` 匹配 ELF 内的源码前缀，`.` 指启动 GDB 时所在的 SDK 根目录；不需要在本机创建 `/workspace`。
烧录的固件必须与打开的 ELF 来自同一次构建。
如修改服务端 `--port`，也要修改 GDB 的 `target remote` 端口；直接使用此初始化脚本仍会连 3333。

正常断开 GDB 后，服务端恢复软件断点的原指令、清除触发器并恢复 CPU 运行。
固件下载后尚未复位，或 Flash 操作失败时，目标保持暂停。若日志提示清理失败，
先重新连接并核对断点、Flash 和寄存器状态；涉及 Flash 恢复时，按 [恢复步骤](06-flash.md#64-恢复中断或失败的写入) 处理。

### 3.2.1 调试 blinky 与复位

下面是交互会话的预期输出，可据此检查源码、单步和复位行为；`rwatch` 对应的变量为一个字节。

```text
(gdb) break blinky_cmsis.c:29
Note: automatically using hardware breakpoints for read-only addresses.
(gdb) continue
Breakpoint 1, blinky_task (arg=<optimized out>) at .../blinky_cmsis.c:29
29          uapi_gpio_toggle(CONFIG_BLINKY_PIN);
(gdb) step
uapi_gpio_toggle (pin=GPIO_02) at .../drivers/drivers/driver/gpio/gpio.c:95
(gdb) finish
Value returned is $1 = 0
(gdb) delete 1
(gdb) rwatch g_gpio_inited
(gdb) continue
Hardware read watchpoint 2: g_gpio_inited
Value = true
(gdb) delete breakpoints
(gdb) reset
reset done, halted at pc=0x00100000
(gdb) x/2i $pc
=> 0x100000:  j  0x100024
```

在复位向量查看指令后，可运行 `break main`、`continue`。
连接可能经历一次整片复位，服务端重连后会报告恢复断点并命中 `main()`。
不要保留 SWD 打开之前的 BootROM/SSB 执行断点跨整片复位，详见 [复位限制](04-debugger.md)。

## 3.3 读回 ROM、Flash 和 RAM

先 `detach` 并停止 GDB 服务端，再运行 dump。输出文件会覆盖，选择新的文件名保留旧备份。
`artifacts/` 用于存放本地生成的文件，已被 Git 忽略。

```bash
cd "$WS63_DEBUG"
mkdir -p artifacts/manual
python3 ws63dump.py rom artifacts/manual/rom.bin
python3 ws63dump.py flash artifacts/manual/flash.bin
python3 ws63dump.py 0xA00000 0x1000 artifacts/manual/sram.bin --halt
```

预期文件大小分别为 311296、4194304 和 4096 字节。
ROM/Flash 经 AHB-AP 读取；RAM 加 `--halt` 后经 CPU 读取，避免写回缓存造成的旧数据。
读完后恢复目标原来的运行/暂停状态。分区地址见 [内存参考](04-debugger.md#48-内存地址)，
逐字节验证方法见 [回归与读回验证](10-verify.md)。

## 3.4 排查连接和调试失败

先检查错误属于探针、固件还是 GDB，避免直接重复烧录。

| 现象 | 处理 |
|---|---|
| `no CMSIS-DAP probe found` | 运行探针枚举；检查 USB 模式和设备权限 |
| `no SWD response` | 检查 GPIO_13/14 接线、供电和 SWD 初始化代码；先尝试 `--speed 1000` |
| DAPLink 只出现 U 盘 | 检查是否进入固件维护模式；确认目标供电，再重新插入探针 |
| `Waiting for device reset: Operation timed out` | 重新执行烧录命令，在提示后及时按复位键 |
| 修改 flashboot 后无效 | 先构建 flashboot，再构建应用；检查包内镜像与 Flash 回读 |
| `still waiting for the debug link` | 可能停在 SWD 尚未开启的代码；复位板卡，删除过早的断点 |
| 无法插入硬件断点/观察点 | 检查 8 个槽位是否已占满；一个数据范围可能占多个槽位，还需为 GDB 临时断点留空，见 [范围观察点](05-watchpoints.md) |
| `PC not saved` | 按 [LiteOS 任务检查](07-rtos.md) 生成带补充 CFI 的 ELF，确认服务端和 GDB 使用同一副本 |
| 变量显示 `<optimized out>` | 当前固件编译优化导致调试信息无法描述变量位置 |
| `finish` 停在意外位置或 RAM 值过旧 | 暂停 CPU 后读取 RAM，并检查 ELF 是否与板上镜像一致；回溯问题见 [任务检查](07-rtos.md) |
| `target cleanup failed` | 断点或寄存器恢复未完成；重新连接并核对现场。若改写过 Flash，先按 [恢复步骤](06-flash.md#64-恢复中断或失败的写入) 确认原指令已恢复，再复位 |

## 3.5 下一步

- [查看 LiteOS 任务与跨任务调用栈](07-rtos.md)，或 [通过 SWD 烧写 Flash](06-flash.md)。
- [运行回归与读回验证](10-verify.md)，检查修改后的调试器。
- [查阅功能和命令限制](04-debugger.md)，选择断点、观察点与复位方式。
- [了解实现原理](09-architecture.md)，维护协议与缓存处理代码。

---

上一篇：[02 在 SDK 中开启 WS63 调试接口](02-enable-swd.md) · [返回总目录](../README.md#教程目录) · 下一篇：[04 调试器功能与限制](04-debugger.md)
