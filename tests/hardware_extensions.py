#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Check software traps and optional blank-sector Flash recovery on a dev board.

The RAM test borrows 16 bytes below SP and restores registers/code. Flash writes
require an explicit address of a disposable, erased 4 KiB sector. Never use a
sector reserved by your firmware even if it happens to be blank.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gdbserver import Server
from ws63dbg import connect, CSR_DCSR, DebugError
from ws63flash import Flash, SECTOR


def ram_breakpoint(hart):
    regs = [hart.read_gpr(i) for i in range(32)]
    pc = hart.pc()
    status, dcsr = hart.read_csr(0x300), hart.read_csr(CSR_DCSR)
    address = (regs[2]-128) & ~3
    if not 0xa00000 <= address < address+16 <= 0xa88000:
        raise DebugError('requires an SRAM task stack')
    old = hart.read_mem(address, 16)
    server = Server(hart)
    try:
        # addi a0,zero,123; j .  Interrupts stay masked only for this tiny test.
        code = bytes.fromhex('1305b0076f000000') + bytes(8)
        hart.write_csr(0x300, status & ~8)
        hart.write_mem(address, code)
        hart.sync_code()
        assert server.handle('Z0,%x,4' % address) == 'OK'
        assert server.software.entries and not server.breakpoints
        assert server.software.read(address, 16) == code
        hart.set_pc(address)
        hart.resume()
        deadline = time.monotonic()+2
        while not hart.halted() and time.monotonic() < deadline:
            time.sleep(.001)
        assert hart.halted() and hart.pc() == address and hart.halt_cause() == 1
        server.step_over()
        assert hart.pc() == address+4 and hart.read_gpr(10) == 123
        assert hart.read_mem(address, 4) == bytes.fromhex('73001000')
        server.software.clear()
        assert hart.read_mem(address, 16) == code
    finally:
        if not hart.halted(): hart.halt()
        server.software.clear()
        hart.write_mem(address, old)
        hart.sync_code()
        for i in range(1, 32): hart.write_gpr(i, regs[i])
        hart.set_pc(pc)
        hart.write_csr(0x300, status)
        hart.write_csr(CSR_DCSR, dcsr)
    assert hart.read_mem(address, 16) == old
    print('RAM trap / displaced step / original bytes and registers restored: PASS', flush=True)


def flash_recovery(hart, address, journal_dir):
    if address % SECTOR: raise DebugError('Flash test address must be sector aligned')
    flash = Flash(hart, journal_dir, print)
    old = flash.read(address, SECTOR)
    if old != b'\xff'*SECTOR: raise DebugError('Flash test requires an erased sector')
    status = flash.status()
    result = flash.apply([(address+0x123, bytes(range(256)))])
    try:
        expected = old[:0x123] + bytes(range(256)) + old[0x223:]
        assert flash.read(address, SECTOR) == expected
        assert flash.read_spi(address+0x123, 256) == bytes(range(256))
    finally:
        flash.restore(result['journal'])
    assert flash.read(address, SECTOR) == old and flash.status() == status
    print('Flash write / both read paths / whole sector and protection restored: PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--speed', type=int, default=4000)
    parser.add_argument('--flash-address', type=lambda s:int(s,0))
    parser.add_argument('--journal-dir', default='artifacts/flash-test')
    args = parser.parse_args()
    dap, hart = connect(args.speed, args.serial)
    was_halted = hart.halted()
    success = False
    try:
        hart.halt()
        ram_breakpoint(hart)
        if args.flash_address is not None:
            flash_recovery(hart, args.flash_address, args.journal_dir)
        success = True
    finally:
        try:
            if success and not was_halted: hart.resume()
            else:
                hart.restore_registers()
                print('target left halted; inspect any failure before resuming')
        finally: dap.close()


if __name__ == '__main__': main()
