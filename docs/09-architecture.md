# 09 WS63 的 SWD 调试实现

本文解释 [调试指南](03-getting-started.md) 中的协议分层、缓存一致性和芯片差异，供维护 Python 调试器时参考。

**本篇目录**

- [9.1 硬件调试通路](#91-硬件调试通路)
- [9.2 软件分层](#92-软件分层)
- [9.3 调试模块特性](#93-调试模块特性)
- [9.4 寄存器缓存](#94-寄存器缓存)
- [9.5 批量传输](#95-批量传输)
- [9.6 故障恢复边界](#96-故障恢复边界)
- [9.7 Flash、软件断点与现场分析](#97-flash软件断点与现场分析)
- [9.8 协议依据](#98-协议依据)

## 9.1 硬件调试通路

WS63 使用 RV32 内核，RISC-V 调试模块通过 ARM CoreSight 调试访问端口（DAP）连接到 SWD：

```text
调试器 ──SWD──> SW-DP (DPIDR 0x5BA02477)
                  ├─ AP0: APB-AP ──> RISC-V Debug Module（规范 0.13，偏移 0） ──> RV32 hart
                  └─ AP1: AHB-AP ──> 系统总线（ROM / Flash XIP / SRAM / TCM / 外设）
```

SWD 传输使用 CoreSight 的调试端口（DP）和访问端口（AP）协议；
控制 CPU 时，还需通过 AP0 实现 RISC-V 调试协议。本工程由 Python 后端完成这些操作，不依赖 OpenOCD。

## 9.2 软件分层

下图按调用关系列出主要模块。`Hart` 表示 WS63 的 RISC-V 硬件执行线程，负责 CPU 状态控制。

```text
gdbserver.py   GDB 远程协议、断点管理、复位处理
    ├─ ws63watch.py   数据范围分配、访存指令解码、命中与边界判定
    │
ws63dbg.Hart   RISC-V 调试协议：暂停/恢复、寄存器、CSR、单步、触发器、内存
    │
ws63dbg.DAP    ADIv5 DP/AP 层：AP 选择、TAR/DRW 访问、AHB-AP 块读
    │
cmsisdap.py    CMSIS-DAP 命令（DAP_Transfer / DAP_TransferBlock），v1 HID 或 v2 bulk
```

接入其他探针时，需实现 `DAP` 定义的寄存器读写、错误清除、重连和关闭接口；
批量传输接口可按探针能力优化。

## 9.3 调试模块特性

从调试模块寄存器实测得到：

| 项目 | 值 |
|---|---|
| 调试规范 | 0.13（dmstatus.version = 2） |
| 认证状态 | `authenticated = 1`，当前连接已获调试访问权限 |
| 抽象命令访问通用寄存器 | 支持（32 位） |
| 抽象命令访问 CSR | **不支持**（cmderr = 2） |
| 系统总线访问（SBA） | 不支持 |
| Program buffer | 可容纳 3 条指令，无隐含 `ebreak`；末尾需显式写入 `ebreak`，剩余 2 条用于调试操作 |
| postexec | 本板支持 `transfer=0` 和 `transfer=1`；本实现使用“读 x0 + postexec”（`0x00261000`） |
| 数据寄存器 | 1 个（data0），hartinfo = 0（没有映射到内存） |
| 硬件触发器 | 8 个，mcontrol（type 2），支持 M/U 模式，不支持 S 模式位；本板未置位 hit 标志 |

应对方法：

- **读写 CSR**：在 program buffer 中写入 `csrr s0, <csr>` 或 `csrw <csr>, s0`，末尾添加 `ebreak`。
  通过“读 x0 + postexec”命令执行，并用抽象命令传递 s0 的值。
- **读写内存（CPU 暂停时）**：program buffer 放 `lw s1,0(s0); addi s0,s0,4`，打开 abstractauto 的
  autoexecdata，中间的 data0 读取会自动取下一个字。末尾关闭自动执行，并单独取出最后两个结果，避免越界多读；写内存同理（`sw s1,0(s0); addi s0,s0,4`）。
- **单步**：用 `csrsi dcsr, 4` 设置 step 位；恢复运行并再次暂停后，用 `csrci dcsr, 4` 清除此位。
- **执行断点**：tdata1 = `0x28001048 | execute`（dmode=1、action=进入调试模式、M、U），tdata2 = 地址。
- **数据观察点**：使用 NAPOT 匹配，将范围表示为按自身长度对齐的 2 的幂大小区间。
  `match=1` 时，对于长度 `N >= 2` 的区间，tdata2 = `起始地址 | (N/2 - 1)`。
  配置后读回检查，硬件拒绝的范围不能作为成功返回给 GDB。

## 9.4 寄存器缓存

CPU 暂停后，首次访问寄存器时批量读取并缓存 x1–x31；x0 恒为零。
访问 CSR 和内存时，后端借用 s0、s1 作为临时寄存器。GDB 读取寄存器时返回缓存值，
恢复运行或单步前再将原值写回 CPU。

## 9.5 批量传输

每个 DM 寄存器访问都要两次 AP 访问（先写 TAR，再读写 DRW）。一连串访问会打包进同一个
`DAP_Transfer` 命令。工具通过 `DAP_Info` 查询命令包长与缓冲包数，并据此限制分包大小和并发数量。
USB 端点最大包长与 CMSIS-DAP 命令包长是不同字段，不能仅凭前者决定命令分包大小。

bulk 接收缓冲区使用协商后的命令包长，每次读取一个响应。若探针返回完整的 512 字节包，
而主机请求读取 1024 字节，USB 读取可能继续等待后续数据，导致两个响应合并或超时。
按 512 字节接收可保留流水线中的响应边界；短响应仍按实际长度处理。
抽象命令的错误标志（cmderr）是粘滞的，所以一个批次只在最后读一次 abstractcs 检查。

## 9.6 故障恢复边界

抽象命令超时或失败会抛出 `DebugError`。Program buffer 的缓存随失败失效；RAM 传输在退出时关闭
`abstractauto`，防止后续访问 `data0` 意外执行遗留指令。恢复运行前必须收到 `allresumeack`。

GDB 数据包先校验校验和、完整性和写入长度，再执行目标操作。
数据观察范围以 4 字节向外对齐后分解为 NAPOT 区间，覆盖从目标字节前面开始的自然对齐宽访问。
本板没有可用的 hit 标志，`ws63watch` 从暂停 PC 的原指令和执行前寄存器推导访问地址、宽度与读写类型。
它区分真实重叠与边界误触发，仅对已解码且不重叠的访问自动单步后继续。
未知指令保持暂停，具体边界见 [功能参考](04-debugger.md#43-数据观察点行为)。

分配范围前先检查完整槽位预算；传输失败后撤销该范围的全部槽位。
若撤销也失败，保留槽位归属并禁止继续执行，避免复用配置未知的硬件触发器。

## 9.7 Flash、软件断点与现场分析

`ws63flash.Flash` 在 hart 暂停时经 AHB-AP 控制 SFC 的命令、地址和 64 字节数据缓冲区。
擦写前备份所有相关扇区，暂时解除保护，擦写后逐字节核对并恢复保护；失败时保留恢复日志。
借用控制器期间还保存并恢复命令寄存器、数据缓冲区和 WEL，避免破坏暂停前固件已经准备的 SFC 操作。
`gdbserver.py` 将 vFlash 请求暂存到主机，收到 `vFlashDone` 后才执行事务。
普通内存写入不能绕过 Flash 编程器。

`ws63break.SoftwareBreakpoints` 保存原指令，向 GDB 提供原字节视图，并在单步期间临时撤下断点。
改写代码后通过厂商缓存维护 CSR 和 fence/fence.i 同步。清理失败时不会继续运行未知代码。

`ws63elf.Image` 从 ELF 获取符号、结构布局和调用帧信息（CFI）。`prepare` 在 ELF 副本中补充已识别函数的固定栈规则，
不改装载节；`ws63rtos.LiteOS` 解码内核保存的任务上下文，未保存的寄存器标为不可用。
`ws63diagnose` 保存寄存器、分页 RAM 和诊断数据；离线服务验证哈希后提供只读视图。
缺失页与不支持的栈操作作为错误或回溯终止原因报告。

## 9.8 协议依据

实现细节以源码和本板实测为准，标准协议的字段定义见：

- [CMSIS-DAP DAP_Transfer](https://arm-software.github.io/CMSIS-DAP/latest/group__DAP__Transfer.html)：传输数量、应答状态及数据顺序。
- [CMSIS-DAP DAP_Info](https://arm-software.github.io/CMSIS-DAP/latest/group__DAP__Info.html)：最大命令包长（`0xFF`）和缓冲包数（`0xFE`）。
- [libusb 同步传输](https://libusb.sourceforge.io/api-1.0/group__libusb__syncio.html)：bulk 读取长度、实际传输长度和超时返回值。
- [GDB Remote Serial Protocol](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Overview.html)：数据包校验、确认与重传。
- [RISC-V Debug 0.13.2](https://riscv.org/wp-content/uploads/2024/12/riscv-debug-release.pdf)：抽象命令、program buffer 和触发器字段。

---

上一篇：[08 捕获现场并离线分析](08-diagnostics.md) · [返回总目录](../README.md#教程目录) · 下一篇：[10 验证调试器修改与 Flash 读回](10-verify.md)
