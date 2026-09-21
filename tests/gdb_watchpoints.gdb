# SPDX-License-Identifier: GPL-2.0-only
# Run with the matching prepared blinky ELF and an already connected server.
set pagination off
set confirm off
set breakpoint always-inserted on
set $gpio_hits = 0
set $register_hits = 0
hbreak blinky_cmsis.c:29
continue
delete breakpoints
rwatch g_gpio_inited
commands
  silent
  set $gpio_hits = $gpio_hits + 1
end
rwatch g_gpios_regs[0]
commands
  silent
  set $register_hits = $register_hits + 1
end
monitor watchpoints
# Keep continue at top level: GDB defers breakpoint command lists inside while.
continue
continue
continue
continue
continue
continue
printf "read-stop counts: gpio=%d register=%d\n", $gpio_hits, $register_hits
monitor watchpoints
if $gpio_hits == 0 || $register_hits == 0
  echo ERROR: both data watchpoints must be identified
  quit 1
end
printf "identified read watchpoints: gpio=%d register=%d\n", $gpio_hits, $register_hits
delete breakpoints
# A word load starts one byte before this unaligned, six-byte watched range.
rwatch -location *((unsigned char (*)[6]) ((char *)&g_gpios_regs[0] + 1))
set $range_hits = 0
commands
  silent
  set $range_hits = $range_hits + 1
end
monitor watchpoints
continue
continue
if $range_hits != 2
  echo ERROR: both partial-overlap reads must be identified\n
  quit 1
end
printf "identified unaligned range reads: %d\n", $range_hits
delete breakpoints
watch g_tickCount
set $tick_hits = 0
set $tick_before = g_tickCount[0]
commands
  set $tick_hits = $tick_hits + 1
end
monitor watchpoints
continue
continue
if $tick_hits != 2 || g_tickCount[0] <= $tick_before
  echo ERROR: both writes to the eight-byte tick count must be identified\n
  quit 1
end
monitor watchpoints
delete breakpoints
monitor triggers
detach
quit
