# 04 调试器功能与限制

本页供已完成 [接线与启动](03-getting-started.md) 的开发者查阅命令、内存范围和调试行为。
以下说明适用于本工程和 BearPi-Pico H3863 SDK v1.0.102 的 blinky 固件。
探针接口与依赖见 [主机准备](01-preparation.md#14-检查探针与访问权限)。

**本篇目录**

- [4.1 GDB 功能](#41-gdb-功能)
- [4.2 monitor 命令](#42-monitor-命令)
- [4.3 数据观察点行为](#43-数据观察点行为)
- [4.4 软件断点](#44-软件断点)
- [4.5 SWD 打开前的断点](#45-swd-打开前的断点)
- [4.6 复位行为](#46-复位行为)
- [4.7 数据缓存](#47-数据缓存)
- [4.8 内存地址](#48-内存地址)
- [4.9 命令行选项](#49-命令行选项)
- [4.10 Python 接口](#410-python-接口)
- [4.11 使用限制](#411-使用限制)

## 4.1 GDB 功能

| 功能 | 说明 |
|---|---|
| `break` / `tbreak` | 可写、可执行 RAM 优先软件断点；ROM/Flash 默认硬件断点；PMP 拒绝 RAM 写入时回退硬件 |
| `hbreak` | 显式硬件断点 |
| `watch` / `rwatch` / `awatch` | 写 / 读 / 读写观察点；支持多个对象、多字节及非对齐范围，见 [使用方法](05-watchpoints.md) |
| `step` / `next` / `stepi` / `nexti` / `finish` / `until` | 源码级或指令级单步、执行到函数返回或指定位置 |
| `continue`，Ctrl-C 暂停 | 恢复运行，或中断运行并返回 GDB 提示符 |
| `bt`、`info locals`、`print` | GDB 使用 ELF 查看调用栈和变量；跨任务回溯需在服务端使用 `--elf`，见 [任务检查](07-rtos.md) |
| `info threads` / `thread` / `thread apply all bt` | 查看与选择任务，非当前任务上下文只读，详见 [任务检查](07-rtos.md) |
| `load` | 通过 SFC 写 Flash，完成后必须复位；签名镜像与 ELF 的区别见 [Flash 烧写](06-flash.md) |
| 寄存器 | 32 个通用寄存器、pc、32 个浮点寄存器、fflags/frm/fcsr，以及 `$mstatus` `$misa` `$mie` `$mtvec` `$mscratch` `$mepc` `$mcause` `$mtval` `$mip` `$dcsr` `$dpc` |
| 内存读写 | `x`、`print *ptr`、`set var` |
| 内存映射 | ROM 为只读，Flash 报告 4096 字节擦除块；`--no-flash` 将 Flash 设为只读。普通 `M/X` 包仍不能直接写 Flash |
| 目标函数调用 | 支持简单同步函数；会执行目标代码，需要匹配的 ELF 和函数类型，见 [调用方法与限制](11-gdb-recipes.md#112-调用目标函数) |

硬件断点和数据观察点共用 **8 个硬件触发器**。GDB 执行 `next`、`finish` 时也可能需要临时断点，应预留槽位。

## 4.2 monitor 命令

| 命令 | 作用 |
|---|---|
| `monitor` | 显示帮助 |
| `monitor reset` / `monitor reset halt` | 复位并停在 `0x100000`；`ws63.gdbinit` 的 `reset` 命令还会刷新 GDB 寄存器缓存 |
| `monitor csr <编号> [值]` | 按编号读写芯片支持的控制状态寄存器（CSR），例如 `monitor csr 0x342` |
| `monitor triggers` | 硬件触发器总数和已用数量 |
| `monitor watchpoints` | 已编程的数据范围、槽位、最后一次访问解码及边界过滤次数 |
| `monitor dcsr` | 显示 dcsr |
| `monitor tasks [water]` / `monitor bt [all\|任务ID]` | 任务列表、水位及服务端 CFI 回溯 |
| `monitor sync` | 互斥锁/信号量等待与候选等待环 |
| `monitor snapshot 目录` | 保存暂停现场，目录必须尚不存在 |
| `monitor exceptions [off\|目录]` | 在 SDK 异常处理入口停下，可自动抓取快照 |
| `monitor flash info` / `monitor flash write 地址 文件` / `monitor flash restore 日志目录` | Flash 型号、写入与恢复；路径相对于服务端工作目录 |
| `monitor md 地址 [数量]` / `monitor mw 地址 值` | 对齐的 32 位外设寄存器读写 |

## 4.3 数据观察点行为

服务端将请求范围分解为硬件可表示的对齐区间。
每个区间占一个槽位，一个观察点可能占多个槽位。8 个槽位由执行断点、数据观察点、异常入口和
GDB 临时断点共享。槽位不足返回 `E0E`，服务端不会留下部分成功的范围。

硬件比较的是访存起始地址。服务端将范围边界扩到 4 字节对齐，再解码触发处指令，按实际访问的
字节范围判断重叠。因此，观察一个字节也能捕获从它前面开始、覆盖它的对齐半字或字访问。
已覆盖 RV32 整数、单精度浮点、对应压缩访存，以及 WS63 的短 `lbu/sb/lhu/sh` 指令。
64 位 C 对象可以用范围观察点监视；RV32 编译器对它的分次访问分别判断。

本板 `tdata1.hit` 不置位，命中来源由指令和执行前寄存器确定，不额外读取被观察的数据。
若访问只落在对齐后多出的边界区域，服务端单步执行一次并继续等待；这仍会短暂停机。
未知、多寄存器或非对齐访存不会自动过滤，而是保持暂停并报告 `SIGTRAP`，用 `monitor watchpoints` 查看原因。
不能保证捕获起点落在硬件范围之外的非对齐或多寄存器宽访问。

执行断点和数据观察点同时作用于同一条指令时优先报告执行断点。
同一次访问匹配多个逻辑观察点时，服务端报告其中一个命中地址；诊断输出保留匹配数量。
GDB 在处理命中时可能暂时移除观察点，因此 `monitor watchpoints` 显示的硬件配置可能为空；
用 `info breakpoints` 查看 GDB 中的定义。配置或清理失败且无法回滚时禁止继续执行，直至成功删除相关观察点。

## 4.4 软件断点

RAM 软件断点保存原指令，写入 `EBREAK` / `C.EBREAK`，同步数据/指令缓存，并在单步和断开时恢复。
GDB 读内存看到原指令；修改断点覆盖的字节会更新保存副本。硬件断点仍可用 `hbreak` 显式请求。
软件断点不占用硬件触发器，但受物理内存保护（PMP）的写权限和执行权限约束。

Flash 默认使用硬件断点。可选的 `--software-flash-breakpoints` 会通过扇区擦写插入软件断点，
设置和恢复方法见 [Flash 软件断点](06-flash.md#65-可选使用-flash-软件断点)。
ROM 指令不能改写，只能使用硬件断点。

## 4.5 SWD 打开前的断点

整片复位后，固件重新执行 SWD 初始化代码，调试口才可用。
如果 CPU 先命中 BootROM 或 SSB 中的执行断点，就无法继续到 SWD 初始化位置，服务端会持续等待重连。
遇到这种情况，复位开发板以清除硬件断点，并删除 GDB 中对应的断点定义。

仅在应用中开启 SWD 时，应用入口至 SWD 初始化之间的代码也受此限制。

## 4.6 复位行为

- **整片复位**（上电、按 RST 键、看门狗）会把 GPIO_13/14 恢复成普通 GPIO，调试连接中断，
  直到 flashboot 重新打开（时间随固件变化）。`continue` 期间遇到这种情况，服务端会一直等待，
  连接恢复后，如果芯片确实复位过，会重新设置断点并提示：
  ```text
  ws63dbg: debug link lost, waiting for the firmware to re-enable SWD...
  ws63dbg: target reset detected, 2 breakpoint(s)/watchpoint(s) restored
  ```
  在 flashboot 中开启 SWD 后，服务端可在应用启动前恢复 `main()` 断点。
- **`monitor reset`（`ws63.gdbinit` 中的 `reset`）** 使用调试模块的 ndmreset。
  它保留引脚复用，并将 CPU 暂停在复位向量 `0x100000`，可用于单步检查 BootROM 入口。
- **ndmreset 不等同于断电上电**：该 SDK 在释放 ndmreset 后可能再次整片复位，
  中间会发生“连接中断 → 检测到复位 → 断点恢复”。在出现应用断点之前保持等待。
  停在复位向量仅表示复位暂停成功，还需继续运行并检查应用断点，确认启动完成。

## 4.7 数据缓存

WS63 的 D-Cache 是写回式的，AHB-AP 访问内存会绕过缓存。服务端的处理方式：

- CPU 暂停时，通过调试模块让 CPU 执行 `lw/sw` 读写 RAM，以保持缓存一致性；
- 当前工具不通过普通内存写入修改 ROM/Flash，读取它们直接走 AHB-AP；
- CPU 运行时经 AHB-AP 读取 RAM，**可能读到尚未写回的旧数据**。检查变量或调用栈前先暂停 CPU。

读取旧的栈返回地址会导致 `finish` 选择错误的返回位置，因此暂停时的缓存一致性不能省略。
SFC 编程和软件断点通过 `Hart.sync_code()` 清理数据缓存、使指令缓存失效并执行屏障，
避免 CPU 继续执行旧指令。

## 4.8 内存地址

以下映射来自 SDK 的 `drivers/boards/ws63/evb/memory_config` 与链接配置。完整范围包含表中起止地址；仅列起始地址的区域需结合链接配置确定长度。

| 区域 | 地址 | 说明 |
|---|---|---|
| BootROM | 0x100000 – 0x108FFF | 36 KiB，复位向量 `0x100000` |
| ROM 库 | 0x109000 – 0x14BFFF | 268 KiB，固化的驱动和协议栈代码 |
| ITCM | 0x14C000 起 | 指令紧耦合内存 |
| DTCM | 0x180000 起 | 数据紧耦合内存 |
| Flash（XIP） | 0x200000 – 0x5FFFFF | 4 MiB，可直接执行代码；工具使用此地址范围烧写 |
| SRAM | 0xA00000 起 | 应用数据、任务栈和运行时代码 |

固件包 `ws63-liteos-app_all.fwpkg` 中各镜像的烧写地址如下。这是镜像列表，不包含所有保留分区；
选择测试扇区前还需核对 SDK 的 `build/config/target_config/ws63/param_sector/param_sector.json`。

| 镜像 | 地址 |
|---|---|
| root_params_sign.bin | 0x200000 |
| ssb_sign.bin | 0x202000 |
| flashboot_backup_sign.bin | 0x210000 |
| flashboot_sign.bin | 0x220000 |
| ws63-liteos-app-sign.bin | 0x230000 |
| ws63_all_nv.bin | 0x5FC000 |

## 4.9 命令行选项

| 工具 | 参数 | 默认值与含义 |
|---|---|---|
| `cmsisdap.py` | 无 | 枚举接口及序列号，不暂停目标 |
| `ws63dbg.py` | `--serial`、`--speed` | 指定探针；SWD 默认 4000 kHz |
| `gdbserver.py` | `--serial`、`--speed`、`--port`、`-v` | 默认 `127.0.0.1:3333`；`-v` 记录协议包 |
| `gdbserver.py` | `--elf`、`--no-rtos` | 加载匹配 ELF，默认启用 LiteOS；后者关闭任务解析 |
| `gdbserver.py` | `--no-flash`、`--flash-journal` | 关闭烧写；恢复日志默认 `artifacts/flash` |
| `gdbserver.py` | `--software-flash-breakpoints` | 允许 Flash 的 Z0 请求修改指令，默认关闭 |
| `ws63flash.py` | `info`、`write`、`verify`、`package`、`restore` | [Flash 烧写与恢复](06-flash.md) |
| `ws63elf.py` / `ws63diagnose.py` | `prepare` / `capture`、`report`、`serve` | [调试 ELF](07-rtos.md) 与 [快照](08-diagnostics.md) |
| `ws63dump.py` | `rom 文件`、`flash 文件`、`起始地址 长度 文件` | 数值支持十进制和 `0x`；长度必须大于 0 |
| `ws63dump.py` | `--halt`、`--serial`、`--speed` | `--halt` 暂停后读取，退出时恢复原来的运行/暂停状态 |

所有工具必须独占探针。先退出 GDB 服务端，再运行 dump 或硬件测试；枚举到多个探针时显式指定序列号。
未指定序列号时选择枚举结果中的第一个接口，bulk 排在 HID 前面。

`ws63dump.py` 会覆盖指定的输出文件；失败时可能留下不完整文件，应按预期字节数和校验结果判断。
不带 `--halt` 时不主动暂停 CPU。已经暂停的目标读取 RAM 时仍走 CPU 路径。

## 4.10 Python 接口

在工程根目录运行下面的只读示例，结束时关闭探针：

```python
import ws63dbg

dap, hart = ws63dbg.connect(speed=4000)
try:
    rom_header = hart.read_mem(0x100000, 16)
    print(rom_header.hex())
finally:
    dap.close()
```

`read_mem(addr, length)` 和 `write_mem(addr, data)` 处理非对齐 RAM，但底层以 32 位字访问；
非对齐写入先读取完整字再修改。不要对 FIFO、读清状态等有访问副作用的 MMIO 使用这种读改写方式。
若关闭连接时要保持目标暂停，先调用 `hart.restore_registers()` 写回临时 GPR。
寄存器和 CSR 操作要求 hart 已暂停；CSR 编号范围是 `0x000..0xfff`。
GDB 内存映射中其他地址标为 RAM 只是允许请求，并不证明这些地址都映射了物理 RAM。

## 4.11 使用限制

移植到其他 SDK 或固件前，先核对以下限制：

| 功能 | 限制 |
|---|---|
| 多寄存器或非对齐访存的观察点命中识别 | 当前只解码自然对齐的单个标量访问；未知指令触发时保持暂停 |
| 任意 SDK/RTOS 配置 | 已验证本教程 LiteOS 固件；结构布局、上下文和回溯须匹配实际固件 |
| 队列、堆、完整异常帧解码 | 当前检查任务、栈水位、互斥锁和信号量；异常快照保留处理入口现场 |
| 冻结其他任务、任务单独执行 | `thread` 只切换视图；恢复 hart 后所有任务由内核调度 |
| ROM 完整调用栈 | 没有符号/CFI 的 ROM 函数仍可能中止回溯 |
| RTT、反向执行、无侵入指令跟踪 | 当前没有对应记录通路；暂停式快照不是实时跟踪 |

---

上一篇：[03 连接 GDB 与基础调试](03-getting-started.md) · [返回总目录](../README.md#教程目录) · 下一篇：[05 观察变量与内存范围](05-watchpoints.md)
