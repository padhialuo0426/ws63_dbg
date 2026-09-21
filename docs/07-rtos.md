# 07 检查 LiteOS 任务和调用栈

本指南在 [GDB 基础连接](03-getting-started.md) 上增加任务列表、跨任务回溯、栈水位和同步对象检查。
适用于本教程的 LiteOS v208.5.0 固件；需要与板上镜像一致、保留符号和 DWARF 的 ELF。
当前任务读取 CPU 实际寄存器，其他任务读取 LiteOS 保存的上下文。

**本篇目录**

- [7.1 准备调试 ELF](#71-准备调试-elf)
- [7.2 查看与选择任务](#72-查看与选择任务)
- [7.3 解释检查结果](#73-解释检查结果)

## 7.1 准备调试 ELF

SDK 的部分预编译函数没有完整的栈回溯信息，部分分支后的 CFA 描述与实际指令不符。
`ws63elf.py prepare` 根据可识别的栈指令和控制流补充缺失信息，并修正能够确认的 CFA 冲突。
它还标明 `OsTaskEntry` 是任务调用栈的结束位置，避免 GDB 把初始上下文误认为调用者。

1. 在工程根目录安装 ELF 分析依赖：

   ```bash
   export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
   export WS63_DEBUG="填入实际的 ws63_dbg 路径"
   cd "$WS63_DEBUG"
   python3 -m venv .venv
   . .venv/bin/activate
   python3 -m pip install -r requirements.txt
   ```

2. 生成用于调试的 ELF 副本：

   ```bash
   python3 ws63elf.py prepare \
       "$WS63_SDK/output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf" \
       artifacts/manual/ws63-debug.elf
   ```

   输出 `Added unwind entries: ...`。输出路径必须尚不存在；重新构建后生成新的副本。
   工具核对所有装载节的地址与字节不变，只修改调试信息；不要把这个 ELF 当作签名烧录包。
   工具自动从 SDK 查找 objdump/objcopy，其他目录可显式传入 `--objdump` 和 `--objcopy`。

3. 使用该 ELF 启动服务端：

   ```bash
   python3 gdbserver.py --elf artifacts/manual/ws63-debug.elf
   ```

4. 在另一终端启动同一个 ELF 的 GDB，从 SDK 根目录映射源码路径：

   ```bash
   export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
   export WS63_DEBUG="填入实际的 ws63_dbg 路径"
   cd "$WS63_SDK"
   tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb \
       -x "$WS63_DEBUG/ws63.gdbinit" "$WS63_DEBUG/artifacts/manual/ws63-debug.elf"
   ```

首次任务检查会比对目标的可执行 Flash 节。固件与 ELF 不符时拒绝解析；这项比对不证明 RAM 尚未被修改。
如只需要符号和异常断点、暂不解析 LiteOS，可追加 `--no-rtos`。

## 7.2 查看与选择任务

```gdb
info threads
thread apply all bt 8
monitor tasks water
monitor bt 15
monitor sync
```

本教程 blinky 固件可枚举 19 个任务，`monitor bt 15` 对应其 LiteOS 任务 ID 15：

```text
task 15 BlinkyTask
  #0 ... LOS_TaskDelay+...
  #1 ... osal_msleep+...
  #2 ... blinky_task+...
  #3 ... OsTaskEntry+...
  stop: task entry
```

任务数量和 ID 会随固件变化。`thread N` 使用 `info threads` 最左列的 GDB 编号；
`monitor bt N` 使用 LiteOS 任务 ID；`monitor tasks` 的 THREAD 列是十六进制远程线程标识。
先看表格再选择，不要将三种编号混用。

在 `info threads` 中找到所需任务后执行 `thread N`，即可用 `bt`、`info registers`、
`frame` 和 `info locals` 查看该任务。选择任务只改变观察视图，不会写 CPU 来模拟任务切换。
非当前任务的寄存器不可写，也不能对该任务直接单步；恢复 hart 后仍由 LiteOS 调度所有任务。

## 7.3 解释检查结果

| 结果 | 含义 |
|---|---|
| `<unavailable>` | 合作式切换没有保存该寄存器；工具不会填入当前任务的值 |
| `<optimized out>` | 编译器未提供该变量在当前位置的可用描述 |
| `STACK USED/MAX` | 根据栈填充值估计历史最大使用量/栈容量，不是当前调用栈深度 |
| `bad stack magic` | 栈底标记不匹配，水位估计无效，应检查越界或布局差异 |
| `mutex_wait_cycles` | 当前互斥锁等待图中的环；只作为排查线索，不能单凭快照认定永久死锁 |
| `no CFI` / ROM 中 `??` | 没有可用的符号或回溯规则；在此停止，不猜测返回地址 |
| `boot/interrupt context` | 内核未调度任务，或当前 SP 与 TCB 的任务栈不一致 |

同步对象检查目前解析任务等待的互斥锁和信号量，显示锁持有者、计数和等待环。
队列、堆分配器、任务冻结和调度控制仍未实现。

补充 CFI 只支持已识别的固定栈操作，包括 WS63 的 push/pop 和 stmia/ldmia；
动态栈调整、无法确认的分支合流和手写调度汇编不会强行生成规则。
异常或中断处的实时寄存器属于处理现场，不能直接当作被打断任务的完整寄存器。
首次调度、调度切换中间态、不同 LiteOS 配置和其他 SDK 仍需单独验证。

下一步：[保存故障现场](08-diagnostics.md)，在没有板卡的电脑上重现任务视图。

---

上一篇：[06 通过 SWD 烧写 Flash](06-flash.md) · [返回总目录](../README.md#教程目录) · 下一篇：[08 捕获现场并离线分析](08-diagnostics.md)
