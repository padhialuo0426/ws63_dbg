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

WS63 是 RV32 内核。它的 RISC-V 调试模块没有用 RISC-V 自己的 JTAG 调试口，
而是**挂在一个标准的 ARM CoreSight DAP 后面**：

```text
调试器 ──SWD──> SW-DP (DPIDR 0x5BA02477)
                  ├─ AP0: APB-AP ──> RISC-V Debug Module（规范 0.13，偏移 0） ──> RV32 hart
                  └─ AP1: AHB-AP ──> 系统总线（ROM / Flash XIP / SRAM / TCM / 外设）
```

所以从调试器的角度看，它和连一颗 STM32 一样：SWD 线上是标准的 ARM DAP。
不同的是 AP 后面挂的是 RISC-V 调试模块，驱动必须实现 RISC-V 调试协议。

本工程通过 CMSIS-DAP 的 DP/AP 读写命令访问 RISC-V 调试模块。
本工程使用 Python 后端，不依赖 OpenOCD。
[HiSpark 的 OpenOCD 分支](https://gitcode.com/HiSpark/vscode-hispark-studio)
另有 `riscvcs` 目标驱动用于这条通路；其功能需要按具体版本分别验证。

## 9.2 软件分层

每层只负责一种协议，硬件差异集中在后端和 Hart 层。

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

要支持别的调试器，只需要实现"读写一个 DP/AP 寄存器"这一层。

## 9.3 调试模块特性

从调试模块寄存器实测得到：

| 项目 | 值 |
|---|---|
| 调试规范 | 0.13（dmstatus.version = 2） |
| 认证 | 不需要（authenticated = 1） |
| 抽象命令访问通用寄存器 | 支持（32 位） |
| 抽象命令访问 CSR | **不支持**（cmderr = 2） |
| 系统总线访问（SBA） | 不支持 |
| Program buffer | 3 条，**没有隐含的 ebreak**（最后一条必须自己放 ebreak，所以只能放 2 条有效指令） |
| postexec | 本板支持 `transfer=0` 和 `transfer=1`；本实现使用“读 x0 + postexec”（`0x00261000`） |
| 数据寄存器 | 1 个（data0），hartinfo = 0（没有映射到内存） |
| 硬件触发器 | 8 个，mcontrol（type 2），支持 M/U 模式，不支持 S 模式位；本板未置位 hit 标志 |

应对方法：

- **读写 CSR**：往 program buffer 里放 `csrr s0, <csr>` 或 `csrw <csr>, s0` 加 `ebreak`，
  用"读 x0 + postexec"命令执行，再用抽象命令读写 s0。
- **读写内存（CPU 暂停时）**：program buffer 放 `lw s1,0(s0); addi s0,s0,4`，打开 abstractauto 的
  autoexecdata，中间的 data0 读取会自动取下一个字。末尾关闭自动执行，并单独取出最后两个结果，避免越界多读；写内存同理（`sw s1,0(s0); addi s0,s0,4`）。
- **单步**：用 `csrsi dcsr, 4` 置位 step，恢复运行，停下后 `csrci dcsr, 4` 清掉，不需要先读 dcsr。
- **执行断点**：tdata1 = `0x28001048 | execute`（dmode=1、action=进入调试模式、M、U），tdata2 = 地址。
- **数据观察点**：另设 `match=1`；对于长度为 `N` 的对齐区间，tdata2 = `起始地址 | (N/2 - 1)`。
  配置后读回检查，硬件拒绝的范围不能作为成功返回给 GDB。

## 9.4 寄存器缓存

CPU 暂停后，第一次访问寄存器时用一个批次读出全部 31 个通用寄存器缓存起来。之后 s0/s1 就当作
临时寄存器使用（访问 CSR、内存时会被改掉），恢复运行或单步之前，再把原值写回去。
gdb 读寄存器直接从缓存返回。

实测验证过：大量读写 CSR 和内存之后，从硬件重新读回的 31 个寄存器和原值完全一致。

## 9.5 批量传输

每个 DM 寄存器访问都要两次 AP 访问（先写 TAR，再读写 DRW）。一连串访问会打包进同一个
`DAP_Transfer` 命令（示例中的 v1 探针每包 64 字节；实际包长通过 DAP_Info 查询）。
多个包会流水线发出；示例探针允许 4 个在途包，工具按 `DAP_Info` 返回值限制并发数量。
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

`ws63elf.Image` 从 ELF 获取符号、结构布局和 CFI。`prepare` 在 ELF 副本中补充可证明的固定栈规则，
不改装载节；`ws63rtos.LiteOS` 只解码内核实际保存的寄存器，不虚构调用者保存寄存器的值。
`ws63diagnose` 保存寄存器、分页 RAM 和诊断数据；离线服务验证哈希后提供只读视图。
缺失页与不支持的栈操作作为错误或回溯终止原因报告。

## 9.8 协议依据

实现细节以源码和本板实测为准，标准协议的字段定义见：

- [CMSIS-DAP DAP_Transfer](https://arm-software.github.io/CMSIS-DAP/latest/group__DAP__Transfer.html)：传输数量、应答状态及数据顺序。
- [GDB Remote Serial Protocol](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Overview.html)：数据包校验、确认与重传。
- [RISC-V Debug 0.13.2](https://riscv.org/wp-content/uploads/2024/12/riscv-debug-release.pdf)：抽象命令、program buffer 和触发器字段。

---

上一篇：[08 捕获现场并离线分析](08-diagnostics.md) · [返回总目录](../README.md#教程目录) · 下一篇：[10 验证调试器修改与 Flash 读回](10-verify.md)
