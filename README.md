# WS63 CMSIS-DAP 调试工具与教程

使用 CMSIS-DAP 探针，通过 SWD 在 Linux 上调试小熊派 BearPi-Pico H3863（WS63）。
工程提供 Python 调试后端和 GDB 服务端，支持断点、单步、数据观察点、Flash 烧写、
LiteOS 任务检查和现场快照。依赖与宿主机环境见 [主机准备](docs/01-preparation.md)。

教程以 BearPi-Pico H3863 SDK v1.0.102 的 blinky 示例为基线。
支持 CMSIS-DAP v1 HID 和 v2 bulk；首次使用先按第 01、02 篇完成接线和固件修改，
已开启 SWD 的板卡可从 [连接 GDB](docs/03-getting-started.md) 开始。
SDK 编译环境搭建与编译方法参考 [小熊派官方开发文档](https://www.bearpi.cn/core_board/bearpi/pico/h3863/)。

## 教程目录

| 页码 | 篇章 | 内容 |
|---|---|---|
| 01 | [硬件接线与主机准备](docs/01-preparation.md) | 宿主机环境、接线、探针权限、路径变量与所需工具 |
| 02 | [在 SDK 中开启 WS63 调试接口](docs/02-enable-swd.md) | 代码添加位置、应用配置、构建顺序与首次串口烧录 |
| 03 | [连接 GDB 与基础调试](docs/03-getting-started.md) | 连接检查、源码调试、单步复位、内存导出与故障排查 |
| 04 | [调试器功能与限制](docs/04-debugger.md) | GDB 和 monitor 命令、观察点行为、缓存、复位与内存映射 |
| 05 | [观察变量与内存范围](docs/05-watchpoints.md) | 多个变量、多字节与非对齐范围、槽位分配与命中诊断 |
| 06 | [通过 SWD 烧写 Flash](docs/06-flash.md) | 签名固件包、单镜像写入、GDB load 与失败恢复 |
| 07 | [检查 LiteOS 任务和调用栈](docs/07-rtos.md) | 调试 ELF、任务视图、跨任务调用栈、栈水位与同步对象 |
| 08 | [捕获现场并离线分析](docs/08-diagnostics.md) | 当前现场、异常入口、离线 GDB 与外设寄存器访问 |
| 09 | [WS63 的 SWD 调试实现](docs/09-architecture.md) | 协议分层、program buffer、寄存器缓存、触发器与故障恢复 |
| 10 | [验证调试器修改与 Flash 读回](docs/10-verify.md) | 离线回归、真机检查、GDB 验证、Flash 比对与观察点测试 |
| 11 | [条件断点与目标函数调用](docs/11-gdb-recipes.md) | 按命中次数暂停、调用目标函数及使用限制 |

## 工程文件

工具脚本位于工程根目录，教程位于 `docs/`，测试脚本位于 `tests/`。

| 文件或目录 | 用途 |
|---|---|
| [cmsisdap.py](cmsisdap.py) | CMSIS-DAP v1 HID / v2 bulk 传输与探针枚举 |
| [ws63dbg.py](ws63dbg.py) | DP/AP、RISC-V 调试模块及连接检查 |
| [gdbserver.py](gdbserver.py) | GDB 远程协议服务端，默认端口 3333 |
| [ws63watch.py](ws63watch.py) | 数据观察范围分配、WS63 指令解码和命中识别 |
| [ws63.gdbinit](ws63.gdbinit) | 源码路径映射、GDB 连接与 reset 命令 |
| [ws63dump.py](ws63dump.py) | ROM、Flash 和 RAM 导出 |
| [ws63flash.py](ws63flash.py) | 经 AHB-AP 控制 SFC 擦写、校验和恢复 Flash |
| [ws63elf.py](ws63elf.py) / [ws63rtos.py](ws63rtos.py) | ELF/DWARF、调用栈信息和 LiteOS 任务上下文 |
| [ws63break.py](ws63break.py) / [ws63diagnose.py](ws63diagnose.py) | 软件断点、现场快照和离线分析 |
| [requirements.txt](requirements.txt) | 可选 ELF/RTOS 功能依赖 |
| [tests/](tests/) | 可复用的离线测试、硬件检查及比对脚本 |

示例生成的日志、内存转储和调试文件统一放在 `artifacts/` 中，该目录已被 Git 忽略。

## 尚未实现的功能

以下是供开发者参与的候选方向，尚未实现，不代表已确定的开发排期。
现有命令的使用边界见 [功能与限制](docs/04-debugger.md#411-使用限制)。

| 功能 | 尚需完成的工作 |
|---|---|
| 多寄存器与非对齐访存的观察点识别 | 扩展 push/pop、ldmia/stmia 等指令解码，验证访问范围和命中归属；未知指令触发时保持暂停 |
| 堆、队列与完整异常帧解析 | 按实际 LiteOS 配置解析布局，处理损坏结构、等待链和嵌套中断 |
| 按 LiteOS 任务自动过滤断点 | 根据当前任务控制命中，处理任务切换、中断及临时断点 |
| 调度锁定与单任务执行 | 为 GDB `scheduler-locking` 提供后端和内核支持，处理任务间锁与事件依赖；`thread` 目前只切换查看对象 |
| 外设寄存器面板 | 提供匹配 WS63 的寄存器描述、字段解码和访问副作用说明；已有 `monitor md/mw` 可按地址读写 |
| RAM 环形日志 / RTT | 定义固件缓冲区协议，处理运行时缓存一致性、读取竞争和溢出 |
| 目标端 CRC 校验 | 实现可恢复现场的目标端计算流程；已有固件包头 CRC 检查，Flash 内容校验仍由主机读回比较 |
| 无符号 ROM 区域的完整回溯 | 补充匹配的符号或展开信息，并验证异常、中断和未知栈操作；现有 CFI 回溯不能覆盖所有 ROM 函数 |
| 空片初始化与其他 Flash 型号 | 增加启动加载器和 SFC 初始化，适配容量、擦除指令及保护位；已有烧写功能要求 SWD/SFC 已初始化 |
| 反向执行、无侵入指令跟踪与时间线 | 先评估芯片跟踪能力或软件记录方案；现有 SWD 读写和暂停式快照不提供执行历史 |

其他 SDK/RTOS 配置仍需适配和验证。CMSIS-DAP v2 已支持包长协商与按探针缓冲包数运行的流水线；
进一步优化吞吐前，需要分别测量 USB、SWD 和目标执行耗时。

## 许可证

本工程采用 **GNU GPL v2.0 only**（`GPL-2.0-only`），完整条款见 [LICENSE](LICENSE)。
外部 SDK 和工具遵循各自的许可证。
