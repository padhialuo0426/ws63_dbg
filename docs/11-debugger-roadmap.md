# 11 扩展状态与边界

本文供维护调试后端和 GDB 服务端的开发者判断功能是否可实现，以及实现顺序。
适用于本教程的 WS63、LiteOS 和匹配 SDK；现有命令见 [功能与限制](04-debugger.md)。

**本篇目录**

- [11.1 已完成的集成](#111-已完成的集成)
- [11.2 GDB 客户端已有的功能](#112-gdb-客户端已有的功能)
- [11.3 尚未实现的扩展](#113-尚未实现的扩展)
- [11.4 线程视图与调度控制](#114-线程视图与调度控制)

## 11.1 已完成的集成

| 功能 | 当前实现 | 使用入口 |
|---|---|---|
| SWD Flash 烧写 | AHB-AP 直接操作 SFC，支持 GD25Q32；保留签名包、整扇区备份、读回与恢复 | [Flash 烧写](06-flash.md) |
| GDB load | Flash 内存映射、vFlash 擦写、二进制转义，下载后要求复位 | [GDB load](06-flash.md#63-使用-gdb-load) |
| LiteOS 任务视图 | ELF/DWARF 布局、线程协议、只读保存寄存器、CFI 回溯与任务入口终止 | [任务检查](07-rtos.md) |
| RAM 软件断点 | 保存/恢复指令、缓存同步、跨断点单步；PMP 拒绝时回退硬件 | [功能参考](04-debugger.md) |
| Flash 软件断点 | 可选 Z0 扇区改写、备份与还原，默认关闭 | [功能参考](04-debugger.md#44-软件断点与剩余边界) |
| 多数据与范围观察点 | 硬件区间分配、宽访问重叠判断、WS63 短指令解码、边界过滤和失败回滚 | [观察变量与内存范围](05-watchpoints.md) |
| 现场诊断 | 异常入口断点、RAM/寄存器快照、只读离线 GDB、栈水位和锁/信号量等待 | [故障现场](08-diagnostics.md) |
| 传输与外设访问 | 二进制 X 写包、无确认模式、明确的对齐 32 位 MMIO 读写 | [故障现场](08-diagnostics.md#84-明确访问外设寄存器) |

烧录器要求 SWD 和 SFC 已由启动固件初始化，不提供空片通用加载器。
补充的 ELF 调试信息不改变固件字节；ROM 无符号区域与未知栈操作仍不能保证回溯。

## 11.2 GDB 客户端已有的功能

无需新增远程协议即可尝试条件断点、忽略前几次命中、断点命令、自动打印与内存导出。
条件通常由主机在目标停机后计算，自动打印再继续也会短暂停机，不是无侵入实时跟踪。
对应机制见 GDB 的 [条件断点](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Conditions.html)
和 [断点命令](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Break-Commands.html)。

在已连接 GDB、加载本教程 blinky ELF 的会话中，可用计数条件在第二次命中时停下：

```gdb
set $hits = 0
break blinky_cmsis.c:29 if ++$hits == 2
continue
print $hits
```

预期 `$hits` 为 `2`。这是主机判断条件；硬件仍只提供执行地址触发器。

目标函数调用会改变目标程序的运行状态。同步的只读查询适合先验证；不要把需要其他任务
配合的阻塞函数当成普通读内存操作。GDB 的调用及中断语义见
[Calling Program Functions](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Calling.html)。

在上述断点处，可调用 SDK 的地址查询函数：

```gdb
print/x (unsigned int)sfc_port_get_sfc_start_addr()
```

本教程固件返回 `0x200000`。显式指定返回类型可用于 ELF 未提供该函数完整类型信息的情况。

## 11.3 尚未实现的扩展

| 功能 | 尚需完成的工作 |
|---|---|
| 观察点的多寄存器指令解码 | 继续覆盖 push/pop、ldmia/stmia 等访存序列及非对齐访问；目前未知指令触发时保持暂停 |
| 堆、队列和异常帧完整解析 | 按实际内核配置解析布局；验证损坏结构、等待链和中断嵌套 |
| 任务限定的自动断点过滤 | 结合当前 TCB 过滤命中，处理调度切换、中断与临时断点；当前可在 GDB 手动检查任务 |
| 外设寄存器面板 | 提供匹配的寄存器描述和访问副作用语义；目前只有明确宽度的读写命令 |
| RAM 环形日志/RTT | 固件缓冲区协议、运行时缓存一致性和溢出处理；暂停读取不能称为实时日志 |
| 目标端 CRC | 独立、可恢复的目标执行通路；当前 Flash 校验通过主机逐字节读回 |
| CMSIS-DAP v2 吞吐优化 | 需要实物探针验证，不能从 v1 的结果推断 v2 性能 |

可参考 OpenOCD 的 [RTOS 支持](https://openocd.org/doc/html/GDB-and-OpenOCD.html#RTOS-Support)
与 [RTT 机制](https://openocd.org/doc/html/General-Commands.html#Real-Time-Transfer-_0028RTT_0029)。
这些是后续实现条件，不是当前命令承诺。

## 11.4 线程视图与调度控制

`thread` 选择观察视图。WS63 的单个 hart 停止时，所有任务都停止；恢复 hart 后由 LiteOS 调度。
GDB 的 `scheduler-locking` 需要后端与内核配合，不能用虚拟线程编号代替调度控制。
冻结其他任务还可能使当前任务等待的锁或事件永远无法完成。
相关语义见 [GDB all-stop mode](https://sourceware.org/gdb/current/onlinedocs/gdb.html/All_002dStop-Mode.html)。

反向执行、无侵入指令跟踪和完整时间线需要额外记录机制或芯片跟踪能力。
当前 CMSIS-DAP/SWD 读写、断点和快照不提供这些功能。

---

上一篇：[10 验证调试器修改与 Flash 读回](10-verify.md) · [返回总目录](../README.md#教程目录)
