#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""Read WS63 memory (ROM, flash, RAM) over SWD into a file.

Examples:
    python3 ws63dump.py rom   out/rom.bin        # BootROM + ROM library, 0x100000-0x14BFFF
    python3 ws63dump.py flash out/flash.bin      # whole 4 MB flash (XIP), 0x200000-0x5FFFFF
    python3 ws63dump.py 0xA00000 0x1000 out/sram.bin --halt

RAM sits behind a write-back data cache: pass --halt so it is read through the CPU
(coherent). ROM and flash are read over the AHB-AP while the CPU keeps running.
"""
import argparse
import sys
import time

import ws63dbg
from ws63dbg import DebugError, ROM_BASE, ROM_END, FLASH_BASE, FLASH_END

REGIONS = {
    "rom": (ROM_BASE, ROM_END - ROM_BASE),
    "flash": (FLASH_BASE, FLASH_END - FLASH_BASE),
}
CHUNK = 0x10000


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", help="'rom', 'flash' or a start address")
    ap.add_argument("args", nargs="+", help="[length] output-file")
    ap.add_argument("--serial", help="CMSIS-DAP serial number, if several are connected")
    ap.add_argument("--speed", type=int, default=4000, help="SWD clock in kHz")
    ap.add_argument("--halt", action="store_true", help="halt the CPU while reading (needed for RAM)")
    a = ap.parse_args()

    if a.what in REGIONS:
        if len(a.args) != 1:
            ap.error("usage: %s %s <output-file>" % (ap.prog, a.what))
        start, length = REGIONS[a.what]
        out = a.args[0]
    else:
        if len(a.args) != 2:
            ap.error("usage: %s <start> <length> <output-file>" % ap.prog)
        try:
            start, length, out = int(a.what, 0), int(a.args[0], 0), a.args[1]
        except ValueError:
            ap.error("start and length must be integers (decimal or 0x-prefixed)")
    if length <= 0 or start < 0 or start + length > 0x100000000:
        ap.error("length must be positive and the range must fit the 32-bit address space")

    try:
        dap, hart = ws63dbg.connect(a.speed, a.serial)
    except DebugError as e:
        sys.exit("connect failed: %s" % e)
    resume_on_exit = False
    try:
        if a.halt and not hart.halted():
            resume_on_exit = True
            hart.halt()
        t0 = time.time()
        with open(out, "wb") as f:
            for off in range(0, length, CHUNK):
                n = min(CHUNK, length - off)
                f.write(hart.read_mem(start + off, n))
                done = off + n
                sys.stderr.write("\r0x%08x  %3d%%  %.0f KB/s" % (start + done, done * 100 // length,
                                                                  done / 1024 / max(time.time() - t0, 1e-3)))
        sys.stderr.write("\n%s: %d bytes from 0x%08x in %.1f s\n" % (out, length, start, time.time() - t0))
    finally:
        try:
            if resume_on_exit:
                hart.resume()
            else:
                hart.restore_registers()
        finally:
            dap.close()


if __name__ == "__main__":
    main()
