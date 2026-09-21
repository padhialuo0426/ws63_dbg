#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify WS63 data ranges, scalar decoding and guard filtering on a dev board.

Borrows 128 bytes below an SRAM task stack and temporarily masks interrupts.
Restores RAM, registers and every trigger. Does not reset or write Flash.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ws63dbg as w
from gdbserver import Server


def check(h):
    regs = [h.read_gpr(i) for i in range(32)]
    pc, mstatus, dcsr = h.pc(), h.read_csr(0x300), h.read_csr(w.CSR_DCSR)
    selected = h.read_csr(w.CSR_TSELECT)
    saved_triggers = []
    for i in range(h.count_triggers()):
        h.write_csr(w.CSR_TSELECT, i)
        saved_triggers.append(h.read_csrs([w.CSR_TDATA1, w.CSR_TDATA2]))
    area = (regs[2]-256) & ~15
    if not 0xa00000 <= area < area+128 <= 0xa88000:
        raise w.DebugError('requires an SRAM task stack')
    old = h.read_mem(area, 128)
    code, data = area, area+64
    server = Server(h)

    def run(instruction, base, value=0x12345678):
        h.halt()
        server.disable_triggers()
        program = instruction + (b'\x01\x00' if len(instruction) == 2 else b'') + bytes.fromhex('6f000000')
        h.write_mem(code, program)
        h.sync_code()
        h.write_gpr(11, base)
        h.write_gpr(15, base)
        h.write_gpr(10, value)
        h.write_gpr(8, value)
        h.set_pc(code)
        server.enable_triggers()
        h.resume()
        deadline = time.monotonic()+1
        while not h.halted() and time.monotonic() < deadline:
            time.sleep(.001)
        if not h.halted():
            h.halt()
            raise AssertionError('expected data trigger did not fire')
        assert h.pc() == code and h.halt_cause() == 2
        return server.stop_reply()

    try:
        for i in range(len(saved_triggers)):
            h.clear_trigger(i)
        h.write_csr(0x300, mstatus & ~8)
        # Multiple independent points; a word starting before a watched byte.
        assert server.handle('Z3,%x,1' % (data+1)) == 'OK'
        assert server.handle('Z3,%x,8' % (data+16)) == 'OK'
        assert len(server.all_triggers()) == 2
        assert run(bytes.fromhex('03a50500'), data) == 'T05rwatch:%x;' % (data+1)
        assert run(bytes.fromhex('9c23'), data+19) == 'T05rwatch:%x;' % (data+19)
        assert run(bytes.fromhex('03a50500'), data+20) == 'T05rwatch:%x;' % (data+20)
        print('multiple points / 1- and 8-byte ranges / partial-overlap word / WS63 lbu: PASS', flush=True)

        # A byte next to the watched byte must be filtered, not reported as a hit.
        assert run(bytes.fromhex('03c50500'), data) == 'S05'
        assert server.skip_watch_guard()
        time.sleep(.003)
        assert not h.halted()
        h.halt()
        assert h.pc() == code+4 and server.watch_filtered == 1
        print('aligned guard false positive executed once and resumed: PASS', flush=True)

        server.watchpoints.clear()
        assert server.handle('Z2,%x,6' % (data+3)) == 'OK'
        assert len(server.all_triggers()) == 2
        assert run(bytes.fromhex('23a0a500'), data) == 'T05watch:%x;' % (data+3)
        assert run(bytes.fromhex('82a3'), data+8) == 'T05watch:%x;' % (data+8)
        print('unaligned 6-byte range / standard sw / WS63 sh: PASS', flush=True)

        server.watchpoints.clear()
        assert server.handle('Z4,%x,4' % data) == 'OK'
        assert run(bytes.fromhex('03a50500'), data) == 'T05awatch:%x;' % data
        assert run(bytes.fromhex('23a0a500'), data) == 'T05awatch:%x;' % data
        print('access watchpoint reports both load and store: PASS', flush=True)
    finally:
        if not h.halted():
            h.halt()
        server.watchpoints.clear()
        h.write_mem(area, old)
        h.sync_code()
        h.set_pc(pc)
        h.write_csr(0x300, mstatus)
        h.write_csr(w.CSR_DCSR, dcsr)
        for i, (control, address) in enumerate(saved_triggers):
            h.write_csrs([(w.CSR_TSELECT, i), (w.CSR_TDATA1, 0),
                          (w.CSR_TDATA2, address), (w.CSR_TDATA1, control)])
        h.write_csr(w.CSR_TSELECT, selected)
        for i in range(1, 32):
            h.write_gpr(i, regs[i])
        h.restore_registers()
    assert h.read_mem(area, 128) == old
    h.restore_registers()
    print('RAM / registers / trigger state restored: PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--speed', type=int, default=4000)
    args = parser.parse_args()
    dap, hart = w.connect(args.speed, args.serial)
    was_halted, success = hart.halted(), False
    try:
        hart.halt()
        check(hart)
        success = True
    finally:
        try:
            if success and not was_halted:
                hart.resume()
            else:
                hart.restore_registers()
                print('target left halted' + ('; inspect failure before resuming' if not success else ' (original state)'))
        finally:
            dap.close()


if __name__ == '__main__':
    main()
