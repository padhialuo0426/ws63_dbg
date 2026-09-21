# 08 捕获现场并离线分析

本指南供需要保存现场、复查异常或共享调试结果的开发者使用。
先按 [LiteOS 任务检查](07-rtos.md) 准备匹配 ELF，并以 `--elf` 启动服务端。
快照写到本地目录，包含寄存器、RAM、固件标识、任务、水位、回溯及读取失败记录。
下文使用 [路径设置](01-preparation.md#12-设置工程与-sdk-路径) 中的两个变量。
从调试器工程根目录启动服务端，使 `monitor` 命令中的 `artifacts/...` 也落在该目录下。

**本篇目录**

- [8.1 捕获当前暂停现场](#81-捕获当前暂停现场)
- [8.2 在异常处理入口停下](#82-在异常处理入口停下)
- [8.3 不连接板卡运行离线 GDB](#83-不连接板卡运行离线-gdb)
- [8.4 访问外设寄存器](#84-访问外设寄存器)

## 8.1 捕获当前暂停现场

在 GDB 中暂停后，指定一个尚不存在的目录：

```gdb
monitor snapshot artifacts/manual/snapshot-1
```

路径相对于服务端工作目录，成功后返回目录名。默认读取 SRAM `0xa00000..0xa87fff`、
DTCM `0x180000..0x187fff` 和 ITCM `0x14c000..0x14ffff`；读取失败的页记录为缺失。
快照不会自动导出全部外设、全部 ROM、Flash 数据区或整个 DTCM。

也可以退出 GDB 服务端后，在工程根目录独占探针捕获：

```bash
cd "$WS63_DEBUG"
python3 ws63diagnose.py capture artifacts/manual/snapshot-2 \
    --elf artifacts/manual/ws63-debug.elf
python3 ws63diagnose.py report artifacts/manual/snapshot-2
```

命令行捕获结束后恢复原来的运行/暂停状态。`report` 输出保存的 JSON；它不访问硬件。

## 8.2 在异常处理入口停下

在已连接的 GDB 会话中设置异常入口断点，并指定快照目录：

```gdb
monitor exceptions artifacts/manual/exceptions
continue
```

服务端占用一个硬件触发器，在匹配 ELF 的 `OsExcHandleEntry` 入口停止，并自动保存快照。
普通中断不会因此触发。命中异常入口后，检查异常寄存器与调用栈：

```gdb
print/x $mepc
print/x $mcause
print/x $mtval
monitor bt all
monitor exceptions off
```

`mepc` 是异常 PC，`mcause` 是原因。CPU 当前 PC/SP 是异常处理入口现场，处理前的全部寄存器
可能已经存入 SDK 异常帧；当前工具没有把异常帧自动转换成原始任务寄存器。
如果继续执行，固件自己的异常处理可能打印、写入故障记录或复位。

不带目录的 `monitor exceptions` 只停机，不自动抓取。`off` 仅释放它自己的触发器。

## 8.3 不连接板卡运行离线 GDB

在工程根目录启动只读服务：

```bash
cd "$WS63_DEBUG"
python3 ws63diagnose.py serve artifacts/manual/snapshot-1 \
    --elf artifacts/manual/ws63-debug.elf --port 3334
```

在另一个已设置两个路径变量的终端中，使用捕获时的同一个 ELF 启动 SDK GDB。
不加载会自动连接 3333 的 `ws63.gdbinit`；从 SDK 根目录映射源码：

```bash
cd "$WS63_SDK"
tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb \
    -q -nx "$WS63_DEBUG/artifacts/manual/ws63-debug.elf" \
    -ex 'set substitute-path /workspace .'
```

在 GDB 内连接离线端口：

```gdb
target remote :3334
info threads
thread apply all bt 8
monitor tasks water
monitor sync
detach
```

离线服务校验 ELF 和每个内存文件的 SHA-256。缺失内存返回读取错误，不补零。
离线服务从匹配的 ELF 补充捕获时核对过的可执行 Flash 内容；其他未捕获区域仍不可读。
写寄存器、写内存、断点、复位和继续执行均被拒绝；离线分析不会打开 USB 探针。

## 8.4 访问外设寄存器

外设寄存器采用内存映射 I/O（MMIO）。普通内存写入可能对非对齐地址执行“先读后写”，
这可能改变读清寄存器等外设的状态。需要固定宽度访问时，使用对齐的 32 位读写命令：

```gdb
monitor md 0x48000210 1
```

示例读取串行 Flash 控制器（SFC）的容量配置寄存器，返回地址和值。`monitor mw 地址 值` 只写一次，
不自动读回；地址必须四字节对齐。读清、写一清零和 FIFO 寄存器须按 SDK 定义访问，
工具不自动解释这些访问副作用。

下一步：[运行回归验证](10-verify.md)，检查快照完整性和只读保护。

---

上一篇：[07 检查 LiteOS 任务和调用栈](07-rtos.md) · [返回总目录](../README.md#教程目录) · 下一篇：[09 WS63 的 SWD 调试实现](09-architecture.md)
