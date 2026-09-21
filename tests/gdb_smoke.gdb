# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
set confirm off
info registers pc sp
info mem
monitor triggers
break blinky_cmsis.c:29
continue
bt 2
info locals
step
finish
next
stepi
delete breakpoints
rwatch g_gpio_inited
continue
print g_gpio_inited
continue
delete breakpoints
reset
if $pc != 0x100000
  echo ERROR: reset vector mismatch\n
  quit 1
end
x/2i $pc
break main
continue
if $pc != &main
  echo ERROR: main breakpoint mismatch\n
  quit 1
end
delete breakpoints
detach
quit
