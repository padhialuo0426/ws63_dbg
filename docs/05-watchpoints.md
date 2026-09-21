# 05 观察变量与内存范围

使用数据观察点定位变量或内存范围的读写位置。开始前完成 [GDB 连接](03-getting-started.md)，
运行本教程的 blinky 固件，并加载匹配的 ELF。命令适用于 SDK 自带的 GDB；变量名来自该 SDK。

**本篇目录**

- [5.1 同时观察两个变量](#51-同时观察两个变量)
- [5.2 观察多字节写入](#52-观察多字节写入)
- [5.3 观察非对齐范围](#53-观察非对齐范围)
- [5.4 判断槽位不足和未知命中](#54-判断槽位不足和未知命中)

## 5.1 同时观察两个变量

先停在 LED 循环，移除用于定位的执行断点，再设置两个读观察点：

```gdb
hbreak blinky_cmsis.c:29
continue
delete breakpoints
rwatch g_gpio_inited
rwatch g_gpios_regs[0]
monitor watchpoints
continue
continue
```

GDB 分别显示两个 `Hardware read watchpoint`。`g_gpio_inited` 是 1 字节布尔量，
`g_gpios_regs[0]` 是 4 字节指针；GPIO 驱动读取它们时会停下。
`monitor watchpoints` 显示请求长度和占用的硬件槽位。

观察点对全体任务有效。启用 LiteOS 视图时，GDB 会显示命中所在任务；选择 `thread` 不会限制观察点的作用范围。
命中后用 `bt` 查看调用链，优化后的变量及无符号 ROM 栈仍可能不可用。

## 5.2 观察多字节写入

删除读观察点，观察内核的 8 字节 tick 计数器：

```gdb
delete breakpoints
watch g_tickCount
monitor watchpoints
continue
continue
```

预期两次显示 `Hardware watchpoint` 及递增的 `Old value`、`New value`。
`watch` 在值变化时报告命中；需要观察读和写两种访问时，使用 `awatch`。
计数器更新频繁，观察它会多次暂停系统，结束后执行 `delete breakpoints`。

## 5.3 观察非对齐范围

下面监视从指针第二个字节开始的 6 字节区域。GPIO 驱动读取整个指针时，
4 字节访问起点在观察范围之前，但两者有重叠，因此仍应停下：

```gdb
delete breakpoints
rwatch -location *((unsigned char (*)[6]) ((char *)&g_gpios_regs[0] + 1))
monitor watchpoints
continue
continue
delete breakpoints
```

将示例中的地址和 `6` 换成需要观察的地址与字节数。地址必须属于实际映射的内存。
此示例的实际访存仍是对齐的；观察范围非对齐不代表支持所有非对齐 CPU 访存。

## 5.4 判断槽位不足和未知命中

芯片有 8 个硬件槽位。一个逻辑范围可能占多个槽位，执行断点和异常入口也消耗槽位；
为 GDB 的临时执行断点保留至少一个空槽，避免 `next` 或观察点处理时无法插入断点。

```gdb
info breakpoints
monitor triggers
monitor watchpoints
```

`info breakpoints` 显示 GDB 的定义；另外两条显示当前硬件配置。
GDB 在暂停处理期间可能临时删除硬件观察点，空配置不代表定义丢失。

硬件覆盖区间可能大于请求范围。服务端确认本次访存没有触及请求范围后，会单步执行该指令并继续运行。
`filtered guard stops` 是这种边界过滤的累计次数。
若显示 `unknown/non-scalar or unaligned access; target kept halted`，目标保持暂停，
用 `x/i $pc` 和 `info registers` 检查指令与地址，再决定是否继续运行。

可解码的指令与重叠观察点的报告方式见 [数据观察点行为](04-debugger.md#43-数据观察点行为)，
复现检查步骤见 [回归验证](10-verify.md#106-验证数据观察点)。

---

上一篇：[04 调试器功能与限制](04-debugger.md) · [返回总目录](../README.md#教程目录) · 下一篇：[06 通过 SWD 烧写 Flash](06-flash.md)
