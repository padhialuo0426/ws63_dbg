# 11 条件断点与目标函数调用

本篇介绍按命中次数暂停程序，以及在暂停时调用目标函数。开始前按 [基础调试](03-getting-started.md)
连接 GDB，并加载与板上 blinky 固件匹配的 ELF。

**本篇目录**

- [11.1 按命中次数暂停](#111-按命中次数暂停)
- [11.2 调用目标函数](#112-调用目标函数)

## 11.1 按命中次数暂停

下面使用 GDB 便利变量记录命中次数，在第二次执行到 LED 翻转语句时暂停。
先删除已有断点，避免其他断点打断示例：

```gdb
delete breakpoints
set $hits = 0
break blinky_cmsis.c:29 if ++$hits == 2
continue
print $hits
```

预期 `$hits` 为 `2`。条件由宿主机上的 GDB 计算：目标每次命中地址断点后都会短暂停机，
条件不满足时 GDB 再恢复运行。因此，条件断点会影响程序时序，不适合作为无侵入实时采样。
条件表达式的语法见 [GDB 条件断点](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Conditions.html)。

## 11.2 调用目标函数

在上面的断点处，可调用 SDK 中查询 Flash 映射起始地址的函数：

```gdb
print/x (unsigned int)sfc_port_get_sfc_start_addr()
```

本教程固件返回 `0x200000`。表达式中的返回类型用于补充 ELF 中可能缺失的函数类型信息。
启用 LiteOS 任务视图时，必须选中当前运行任务，不能从其他任务的只读上下文发起调用。

函数调用会恢复 CPU 并执行目标代码。仅对已了解行为的同步函数使用此功能；
需要其他任务配合的阻塞调用、复位函数或引发任务切换的调用，可能无法恢复原来的调试现场。
详细语义见 [GDB 目标函数调用](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Calling.html)。

结束后清除断点并断开连接：

```gdb
delete breakpoints
detach
quit
```

需要定位变量访问位置时，继续阅读 [数据观察点](05-watchpoints.md)；
需要查看其他任务的状态时，阅读 [LiteOS 任务检查](07-rtos.md)。

---

上一篇：[10 验证调试器修改与 Flash 读回](10-verify.md) · [返回总目录](../README.md#教程目录)
