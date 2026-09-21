# WS63 CMSIS-DAP 调试工具与教程

使用 CMSIS-DAP 探针，通过 SWD 在 Linux 上调试小熊派 BearPi-Pico H3863（WS63）。
工程提供 Python 调试后端、GDB 服务端、SWD Flash 烧写、LiteOS 任务检查及现场快照。
支持硬件/软件断点、单步、多数据/范围观察点和复位重连。基础调试使用 Python 标准库；
ELF/RTOS 功能需要 pyelftools，CMSIS-DAP v2 后端需要系统 libusb-1.0。

教程以 BearPi-Pico H3863 SDK v1.0.102 的 blinky 示例为基线。
CMSIS-DAP v1 HID 已完成真机验证，v2 bulk 尚未真机验证；使用前必须在固件中开启 SWD。
SDK 编译环境搭建与编译方法参考 [小熊派官方开发文档](https://www.bearpi.cn/core_board/bearpi/pico/h3863/)。

## 教程目录

| 页码  | 篇章                                           | 内容                                 |
| --- | -------------------------------------------- | ---------------------------------- |
| 01  | [硬件接线与主机准备](docs/01-preparation.md)          | 接线、探针权限、路径变量与构建烧录工具                |
| 02  | [在 SDK 中开启 WS63 调试接口](docs/02-enable-swd.md) | 代码添加位置、应用配置、构建顺序与首次串口烧录            |
| 03  | [连接 GDB 与基础调试](docs/03-getting-started.md)   | 连接检查、源码调试、单步复位、内存导出与故障排查           |
| 04  | [调试器功能与限制](docs/04-debugger.md)              | GDB 和 monitor 命令、观察点行为、缓存、复位与内存映射  |
| 05  | [观察变量与内存范围](docs/05-watchpoints.md)          | 多个变量、多字节与非对齐范围、槽位分配与命中诊断           |
| 06  | [通过 SWD 烧写 Flash](docs/06-flash.md)          | 签名固件包、单镜像写入、GDB load 与失败恢复         |
| 07  | [检查 LiteOS 任务和调用栈](docs/07-rtos.md)          | 调试 ELF、任务视图、跨任务调用栈、栈水位与同步对象        |
| 08  | [捕获现场并离线分析](docs/08-diagnostics.md)          | 当前现场、异常入口、离线 GDB 与明确的 MMIO 访问      |
| 09  | [WS63 的 SWD 调试实现](docs/09-architecture.md)   | 协议分层、program buffer、寄存器缓存、触发器与故障恢复 |
| 10  | [验证调试器修改与 Flash 读回](docs/10-verify.md)       | 离线回归、真机检查、GDB 验证、Flash 比对与观察点测试    |
| 11  | [扩展状态与边界](docs/11-debugger-roadmap.md)       | 已实现功能、GDB 客户端能力及后续扩展条件             |

## 工程文件

调试工具位于根目录，文档集中在扁平的 `docs/` 目录。

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

测试脚本随工程保留；日志、转储、固件及本地补丁不入库。生成的验证文件放在已忽略的 `artifacts/` 中。

## 许可证

本工程采用 **GNU GPL v2.0 only**（`GPL-2.0-only`），完整条款见 [LICENSE](LICENSE)。
外部 SDK 和工具遵循各自的许可证。
