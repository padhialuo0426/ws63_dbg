# 10 验证调试器修改与 Flash 读回

本篇面向修改调试器代码或探针固件的维护者。使用调试器完成日常开发时，可跳过这些回归测试。
开始前完成 [硬件准备](01-preparation.md)、[固件构建与烧录](02-enable-swd.md) 和 [GDB 连接](03-getting-started.md)。
离线测试不需要板卡；运行硬件测试前退出其他调试客户端，让脚本独占探针。
日志、内存转储和现场记录保存在已被 Git 忽略的 `artifacts/` 下。

**本篇目录**

- [10.1 运行离线回归](#101-运行离线回归)
- [10.2 验证寄存器与缓存一致性](#102-验证寄存器与缓存一致性)
- [10.3 验证 GDB 与复位后断点](#103-验证-gdb-与复位后断点)
- [10.4 验证内存导出](#104-验证内存导出)
- [10.5 验证软件断点与现场分析](#105-验证软件断点与现场分析)
  - [10.5.1 RAM 软件断点与扇区恢复](#1051-ram-软件断点与扇区恢复)
  - [10.5.2 Flash 软件断点](#1052-flash-软件断点)
  - [10.5.3 任务、快照与固件下载](#1053-任务快照与固件下载)
- [10.6 验证数据观察点](#106-验证数据观察点)
- [10.7 下一步](#107-下一步)

## 10.1 运行离线回归

在工程根目录检查语法与故障处理。测试使用内存中的调试模块模型及模拟传输，不打开 USB 设备。

```bash
export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
export WS63_DEBUG="填入实际的 ws63_dbg 路径"
cd "$WS63_DEBUG"
python3 -m py_compile *.py tests/*.py
python3 -m unittest discover -s tests -v
```

预期汇总结果为 `OK`。测试覆盖传输边界、错误处理、寄存器与 RAM 恢复、观察点、Flash 事务、
软件断点、调用栈信息和离线快照。具体用例见 `tests/test_*.py`，协议依据见 [实现原理](09-architecture.md#98-协议依据)。

## 10.2 验证寄存器与缓存一致性

先停止 GDB 服务端，再运行下面的硬件测试。它暂停 CPU，临时修改栈指针下方的 16 字节范围和 f0，
恢复这些值后执行一条指令，最后恢复原来的运行/暂停状态。测试不写 Flash。

```bash
cd "$WS63_DEBUG"
python3 tests/hardware_smoke.py
```

预期输出包括 `unaligned cached RAM write/read/restore: PASS`、
`31 hardware GPRs preserved after memory/CSR/FPR access: PASS`，以及单步后的 `cause=4`。
该测试依赖本教程 blinky 固件的 SRAM 栈布局，移植到其他固件前需核对借用的内存范围。

## 10.3 验证 GDB 与复位后断点

先在终端 1 启动服务端，然后在终端 2 使用与板上镜像对应的 ELF 执行测试脚本。

终端 1：

```bash
cd "$WS63_DEBUG"
python3 gdbserver.py
```

终端 2（重新设置路径变量）：

```bash
export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
export WS63_DEBUG="填入实际的 ws63_dbg 路径"
cd "$WS63_SDK"
timeout 35 tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb \
    -q -batch -x "$WS63_DEBUG/ws63.gdbinit" \
    output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf \
    -x "$WS63_DEBUG/tests/gdb_smoke.gdb"
```

预期命中 blinky、单步进入 GPIO 函数并返回、两次命中读观察点，随后复位到 `0x100000`，
自动重连并命中 `main()`，最后 detach。脚本检查复位向量和 `main()` 地址；超时退出码为 124，不算通过。
结束后在终端 1 按 Ctrl-C 停止服务端，释放探针。

## 10.4 验证内存导出

CMSIS-DAP v2 探针升级后，先独占探针验证 USB 包边界与重复读回：

```bash
cd "$WS63_DEBUG"
python3 tests/hardware_bulk.py --expect-packet-size 512
```

该命令要求探针通过 `DAP_Info` 报告 512 字节包长；64 字节固件改用 `--expect-packet-size 64`。
测试用重复写入相同值的 DP SELECT 操作填满一个 OUT 包，再重复读取 ROM 与应用 Flash 前 64 KiB，
核对完整 IN 包、末尾短包和流水线响应。它不暂停或复位 CPU，也不擦写 Flash。
预期输出包含 `full-length bulk OUT command: PASS`、两项 `"repeat_equal": true`，
以及 `full-length / short / pipelined bulk IN responses: PASS`。
记录中的耗时仅用于同条件比较，不代表所有调试操作的速度。

使用独占探针读取两份 ROM 和一份 Flash。RAM 读取加 `--halt`，以包含 CPU 缓存中的值。

```bash
cd "$WS63_DEBUG"
mkdir -p artifacts/manual
python3 ws63dump.py rom artifacts/manual/rom.bin
python3 ws63dump.py rom artifacts/manual/rom-repeat.bin
cmp artifacts/manual/rom.bin artifacts/manual/rom-repeat.bin
python3 ws63dump.py flash artifacts/manual/flash.bin
python3 ws63dump.py 0xA00000 0x1000 artifacts/manual/sram.bin --halt
python3 tests/verify_flash.py "$WS63_SDK" artifacts/manual/flash.bin
```

`cmp` 无输出且返回 0 表示两次 ROM 读取一致。Flash 验证输出 6 项 `matches_flash: true`，并返回 0。
比较脚本针对 v1.0.102 的固定分区表；更改 SDK 分区后必须更新脚本。它校验六份镜像覆盖的字节，
不能证明未使用区、NV 运行时更新或其他固件布局与此基线一致。

## 10.5 验证软件断点与现场分析

以下检查分别覆盖指令恢复、任务解析和故障现场。根据修改范围选择对应测试。

### 10.5.1 RAM 软件断点与扇区恢复

停止其他服务端后，验证 RAM 软件断点和现场恢复：

```bash
cd "$WS63_DEBUG"
python3 tests/hardware_extensions.py
```

预期输出 `RAM trap / displaced step / original bytes and registers restored: PASS`。
此测试会临时屏蔽中断、改写 SP 下方 16 字节并执行测试指令，成功后还原状态。
其可选 `--flash-address` 参数会擦写指定扇区，仅供已经核对分区表的空闲测试区使用；
脚本检查该扇区全为 `0xff`，但全空不代表没有被固件预留。

### 10.5.2 Flash 软件断点

先按 [内存导出](#104-验证内存导出) 备份完整 Flash，
并按 [任务检查](07-rtos.md) 准备匹配的 `artifacts/manual/ws63-debug.elf`。
在终端 1 启动允许 Flash 软件断点的服务端：

```bash
cd "$WS63_DEBUG"
python3 gdbserver.py --software-flash-breakpoints
```

终端 2 设置相同的路径变量，然后运行：

```bash
cd "$WS63_DEBUG"
"$WS63_SDK/tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb" \
    -q -nx -batch artifacts/manual/ws63-debug.elf \
    -ex 'set remotetimeout 600' \
    -ex "set substitute-path /workspace $WS63_SDK" \
    -ex 'target extended-remote localhost:3333' \
    -x tests/gdb_flash_breakpoints.gdb
```

脚本临时覆盖 GDB 的 Flash 内存属性，在 blinky 代码中插入真实软件断点，检查 EBREAK 命中、
GDB 单步、再次命中和服务端跨断点硬件单步，最后删除断点并恢复客户端设置。
SDK GDB 的单步可能使用临时软件断点，不能一律要求 `dcsr.cause=4`；脚本分别验证两种路径。
预期输出包括 `Flash software breakpoint reinsertion: PASS`、
`Flash displaced hardware single-step: PASS`，硬件触发器占用为 0，GDB 返回 0。
测试会擦写应用代码扇区；停止服务端后重新导出 Flash，与备份比较，确认原指令恢复。
失败时保留 `artifacts/flash/` 恢复日志，按 [恢复写入](06-flash.md#64-恢复中断或失败的写入) 处理。

### 10.5.3 任务、快照与固件下载

按 [任务检查](07-rtos.md) 生成调试 ELF 并执行 `info threads`、`thread apply all bt 8`、
`monitor tasks water` 和 `monitor sync`。非当前任务的未保存寄存器应显示不可用，写寄存器应被拒绝。
按 [现场捕获](08-diagnostics.md) 保存快照，再从 3334 端口连接离线 GDB，核对任务与调用栈；
离线写内存、单步和继续运行应被拒绝，缺失页不能显示为零。

Flash 验证使用 [烧写指南](06-flash.md) 中的完整签名包；需要验证真实擦写而非相同内容跳过时，
给 `package` 追加 `--force`，随后复位并检查启动、重新读回全部 Flash。
GDB `load` 验证应使用有备份的测试分区或正确的签名镜像，避免覆盖签名头后直接运行。

## 10.6 验证数据观察点

先与 SDK 反汇编器比对全部短标量访存编码，不需要连接板卡：

```bash
cd "$WS63_DEBUG"
python3 tests/verify_watch_decoder.py \
    "$WS63_SDK/tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-objdump"
```

预期输出 `24,512 WS63 scalar-memory encodings match SDK objdump; no extra encodings accepted: PASS`。
比对范围是短标量访存编码，不代表全部 CPU 指令已实现或做过真机逐条执行。

停止其他调试服务后，运行受控硬件测试：

```bash
cd "$WS63_DEBUG"
python3 tests/hardware_watchpoints.py
```

它借用 SP 下方 128 字节，暂时屏蔽中断并执行少量测试指令，验证多观察点、1/8 字节范围、
非对齐 6 字节范围、宽访问部分重叠、WS63 短访存、读写观察点，以及边界误触发只执行一次。
最后输出 `RAM / registers / trigger state restored: PASS`，恢复原来的运行/暂停状态；失败时保持暂停。
测试要求本教程的 SRAM 栈布局，不复位、不擦写 Flash。

按 [任务检查](07-rtos.md) 准备 `artifacts/manual/ws63-debug.elf` 后，在终端 1 启动带 LiteOS 视图的服务端：

```bash
cd "$WS63_DEBUG"
python3 gdbserver.py --elf artifacts/manual/ws63-debug.elf --no-flash
```

终端 2 设置同样的 `WS63_DEBUG`、`WS63_SDK` 路径变量，再执行：

```bash
cd "$WS63_SDK"
timeout 60 tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb \
    -q -nx -batch "$WS63_DEBUG/artifacts/manual/ws63-debug.elf" \
    -ex 'set remotetimeout 20' \
    -ex 'set substitute-path /workspace .' \
    -ex 'target extended-remote localhost:3333' \
    -x "$WS63_DEBUG/tests/gdb_watchpoints.gdb"
```

预期两个读观察点均被识别，输出 `identified unaligned range reads: 2`，
8 字节 `g_tickCount` 连续两次显示递增值，最后槽位占用为 0 并 detach。
脚本对命中计数作断言；返回 0 才算通过。服务端追加 `--no-rtos` 可验证关闭任务视图的路径。
结束后停止终端 1 的服务端释放探针。

## 10.7 下一步

- [查阅调试器限制](04-debugger.md)，区分功能不支持和真实故障。
- [了解协议与缓存处理](09-architecture.md)，定位回归失败涉及的实现层。

---

上一篇：[09 WS63 的 SWD 调试实现](09-architecture.md) · [返回总目录](../README.md#教程目录) · 下一篇：[11 条件断点与目标函数调用](11-gdb-recipes.md)
