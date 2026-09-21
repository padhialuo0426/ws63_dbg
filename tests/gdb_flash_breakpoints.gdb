# SPDX-License-Identifier: GPL-2.0-only
# Matching blinky ELF, --software-flash-breakpoints server, Flash backup required.
# This test modifies the running application's Flash sector, then restores it.
set pagination off
set confirm off
set breakpoint always-inserted on
# This server's automatic memory map lists Flash as region 3 (Flash type).
# Override the client view so GDB sends Z0; the server still programs via SFC.
info mem
delete mem 3
mem 0x200000 0x600000 rw
set breakpoint auto-hw off
break blinky_cmsis.c:29
continue
set $flash_break_pc = $pc
if (($dcsr >> 6) & 7) != 1
  echo ERROR: expected EBREAK halt, not a hardware trigger\n
  quit 1
end
printf "Flash software breakpoint hit at 0x%x, cause=1\n", $pc
monitor triggers
x/i $pc
set $first_half = *(unsigned short *)$pc
set $next_pc = $pc + (($first_half & 3) == 3 ? 4 : 2)
stepi
# The SDK GDB implements stepi with a temporary software breakpoint (cause 1).
# Other GDB versions may use the server's hardware single-step (cause 4).
if $pc != $next_pc || ((($dcsr >> 6) & 7) != 1 && (($dcsr >> 6) & 7) != 4)
  echo ERROR: GDB instruction step failed\n
  quit 1
end
printf "GDB step over Flash breakpoint: PASS, pc=0x%x\n", $pc
continue
if $pc != $flash_break_pc || (($dcsr >> 6) & 7) != 1
  echo ERROR: reinserted Flash software breakpoint did not hit\n
  quit 1
end
echo Flash software breakpoint reinsertion: PASS\n
# Exercise the server's displaced hardware step too, while Z0 remains inserted.
maintenance packet s
maintenance flush register-cache
if $pc != $next_pc || (($dcsr >> 6) & 7) != 4
  echo ERROR: server displaced single-step failed\n
  quit 1
end
echo Flash displaced hardware single-step: PASS\n
delete breakpoints
monitor triggers
mem auto
set breakpoint auto-hw on
detach
quit
