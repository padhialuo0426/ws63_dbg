# 06 通过 SWD 烧写 Flash

本指南适用于已按 [开启调试接口](02-enable-swd.md) 烧录固件、且 SWD 已可连接的开发板。
烧录器支持本板的 **GD25Q32、4 MiB、4096 字节扇区**。工具暂停 CPU，经 AHB-AP 控制 SFC，
不调用旧固件中的固定函数地址，也不向目标下载 RAM 烧录程序。

首次开启 SWD 仍使用串口烧录。SFC 必须已初始化并将 CS1 映射到 `0x200000`；
工具不支持空片初始化，也不自动支持其他 Flash 型号。
本文的 `python3 ws63flash.py` 是本仓库的 SWD 工具，外部 `ws63flash` 命令是串口烧录器。

**本篇目录**

- [6.1 烧写 SDK 签名包](#61-烧写-sdk-签名包)
- [6.2 写入单个镜像或数据](#62-写入单个镜像或数据)
- [6.3 使用 GDB load](#63-使用-gdb-load)
- [6.4 恢复中断或失败的写入](#64-恢复中断或失败的写入)

## 6.1 烧写 SDK 签名包

1. 退出 GDB 服务端，让烧录工具独占探针；在工程根目录检查 Flash：

   ```bash
   export WS63_SDK="填入实际的 bearpi-pico_h3863 SDK 路径"
   export WS63_DEBUG="填入实际的 ws63_dbg 路径"
   cd "$WS63_DEBUG"
   python3 ws63flash.py info
   ```

   本板输出 `JEDEC 0x1640c8, 4 MiB, sector 4096`。不匹配时工具拒绝擦写。

2. 烧写 SDK 生成的完整包：

   ```bash
   python3 ws63flash.py package \
       "$WS63_SDK/output/ws63/fwpkg/ws63-liteos-app/ws63-liteos-app_all.fwpkg"
   ```

   工具检查包头 CRC、范围、长度及重叠，跳过串口 RAM loader，写入六份 Flash 镜像。
   签名与启动头按原样保留；工具不重新签名，也不代替 BootROM 校验签名。
   默认跳过内容相同的扇区，`package` 或 `write` 后追加 `--force` 可强制重写相同内容。

3. 确认输出包含 `verified` 和恢复日志路径，然后按开发板 RST 键重新启动。

   写入后 CPU 保持暂停，不能从烧录前的 PC 直接继续运行新固件。
   完整签名包必须仍包含开启 SWD 的 flashboot，否则复位后调试口可能关闭。

## 6.2 写入单个镜像或数据

所有地址都是 CPU 的 XIP 地址，不是从零开始的 Flash 偏移。下面只更新应用签名镜像：

```bash
cd "$WS63_DEBUG"
python3 ws63flash.py write 0x230000 \
    "$WS63_SDK/output/ws63/acore/ws63-liteos-app/ws63-liteos-app-sign.bin"
python3 ws63flash.py verify 0x230000 \
    "$WS63_SDK/output/ws63/acore/ws63-liteos-app/ws63-liteos-app-sign.bin"
```

成功校验输出 `verify: PASS`。`write` 会保存同一扇区内未覆盖的字节；不同布局或容量需先核对
[分区表](04-debugger.md#48-内存地址)。`--resume` 仅供确认不会改变运行代码的数据写入使用，
它恢复原 PC，不能替代固件更新后的复位。

## 6.3 使用 GDB load

服务端默认报告 Flash 类型和 4096 字节擦除块，并支持 `vFlashErase`、`vFlashWrite`、
`vFlashDone`。`--no-flash` 可关闭烧写并将该区域报告为只读。

**普通应用 ELF 不包含完整签名烧录包。** 对它执行 `load` 虽然能写入 ELF 的装载段，
但不保证签名头、启动元数据和 Flash 中的应用相匹配。更新固件优先使用上面的 `package`。
若要通过 GDB 下载已经签名的应用，可先把签名二进制包成保留原始字节的 ELF 容器：

```bash
cd "$WS63_DEBUG"
mkdir -p artifacts/manual
export WS63_OBJCOPY="$WS63_SDK/tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-objcopy"
"$WS63_OBJCOPY" -I binary -O elf32-littleriscv -B riscv \
    --rename-section .data=.text,alloc,load,readonly,code,contents \
    --change-addresses=0x230000 \
    "$WS63_SDK/output/ws63/acore/ws63-liteos-app/ws63-liteos-app-sign.bin" \
    artifacts/manual/signed-app.elf
python3 gdbserver.py
```

在另一个终端按 [路径设置](01-preparation.md#11-设置工程与-sdk-路径) 配置两个变量，
再从本工程根目录启动 SDK 的 GDB，让 `load` 的相对路径指向刚生成的文件：

```bash
cd "$WS63_DEBUG"
"$WS63_SDK/tools/bin/compiler/riscv/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-gdb" -q -nx
```

在 GDB 中执行：

```gdb
set remotetimeout 600
target remote :3333
load artifacts/manual/signed-app.elf
monitor reset halt
maintenance flush register-cache
continue
```

烧写进度显示在服务端终端。较大镜像可能需要数分钟，`ws63.gdbinit` 已设置 600 秒超时。
GDB 下载还会修改 PC，因此即使镜像完全相同、没有实际擦写，也必须复位后再运行。
容器的入口地址是签名头地址，不能直接跳转执行。

GDB 的擦除请求会把请求扇区中未下载的部分置为 `0xff`；这与命令行 `write` 保留未覆盖字节的行为不同。
协议定义见 [GDB Flash 数据包](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Packets.html)
和 [内存映射](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Memory-Map-Format.html)。

## 6.4 恢复中断或失败的写入

第一次擦除之前，工具将所有待改写扇区的旧内容、SHA-256 和原写保护状态保存到磁盘，
默认位于 `artifacts/flash/`。成功操作也保留备份；临时解除的保护位会恢复，QE 位保留。
这不是掉电原子更新：断电、USB 失联或主机进程被终止时，Flash 可能只有部分扇区写完。

正常捕获到错误时工具保持 CPU 暂停，当前 GDB 会话拒绝继续执行。停止服务端后，把实际日志路径传给恢复命令：

```bash
cd "$WS63_DEBUG"
export WS63_JOURNAL="artifacts/flash/替换为本次输出的目录名"
python3 ws63flash.py restore "$WS63_JOURNAL"
```

恢复命令先校验全部备份，再写回旧扇区并读回校验。若断电后 SWD 无法重新开启，使用串口烧录恢复。
进程重启不会自动查找未完成事务，也不会自动选择恢复哪个镜像；保留并核对本次日志目录。
这些备份和日志均不入库。

工具还恢复借用的 SFC 命令寄存器、数据缓冲区和写使能锁存位，避免影响暂停时尚未完成的固件调用。

下一步：[检查 LiteOS 任务](07-rtos.md)，或按 [回归与读回验证](10-verify.md) 校验修改后的调试器。

---

上一篇：[05 观察变量与内存范围](05-watchpoints.md) · [返回总目录](../README.md#教程目录) · 下一篇：[07 检查 LiteOS 任务和调用栈](07-rtos.md)
