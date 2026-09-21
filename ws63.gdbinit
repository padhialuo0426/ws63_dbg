# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
# Run gdb from the SDK root (source paths are resolved relative to it), with gdbserver.py running:
#   riscv32-linux-musl-gdb -x "$WS63_DEBUG/ws63.gdbinit" output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf
set pagination off
# Map /workspace to the local SDK root if that is the build-time source path.
# Adjust the old path below when the ELF records a different source location.
set substitute-path /workspace .
# Flash programming can take minutes with a CMSIS-DAP v1 probe.
set remotetimeout 600
# Prefer hardware triggers for ROM/Flash; writable RAM can use software traps.
set breakpoint auto-hw on
target remote :3333

# Reset the chip and stop at the reset vector (0x100000), before the BootROM runs.
define reset
  monitor reset halt
  maintenance flush register-cache
end
document reset
Reset WS63 and halt at the reset vector; breakpoints are kept.
end
