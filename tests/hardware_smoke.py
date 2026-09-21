#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""WS63 hardware check. Halts the CPU, temporarily writes 16 stack bytes and f0,
restores them, steps one instruction, and restores the initial run/halt state.
Run only with exclusive access to a development board and its CMSIS-DAP probe.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ws63dbg as w


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--serial')
    ap.add_argument('--speed', type=int, default=4000)
    args = ap.parse_args()
    dap, h = w.connect(args.speed, args.serial)
    was_halted = h.halted()
    try:
        h.halt()
        original = [h.read_gpr(n) for n in range(32)]
        print('DPIDR=%08x pc=%08x sp=%08x triggers=%d' %
              (dap.dpidr(), h.pc(), original[2], h.count_triggers()))
        assert dap.dpidr() == 0x5BA02477
        assert h.count_triggers() == 8
        # Borrow memory below SP while the CPU is stopped; restore before stepping.
        addr = (original[2] - 128) & ~3
        assert 0xA00000 <= addr < 0xA90000, 'unexpected stack address'
        saved = h.read_mem(addr, 16)
        try:
            h.write_mem(addr + 1, b'\x01\x23\x45\x67\x89')
            assert h.read_mem(addr, 16) == saved[:1] + b'\x01\x23\x45\x67\x89' + saved[6:]
        finally:
            h.write_mem(addr, saved)
        assert h.read_mem(addr, 16) == saved
        print('unaligned cached RAM write/read/restore: PASS')
        mstatus = h.read_csr(0x300)
        try:
            h.write_csr(0x300, mstatus | 0x2000)
            saved_f0 = h.read_fpr(0)
            try:
                h.write_fpr(0, 0x3F800000)
                assert h.read_fpr(0) == 0x3F800000
            finally:
                h.write_fpr(0, saved_f0)
        finally:
            h.write_csr(0x300, mstatus)
        print('FPR and CSR write/read/restore: PASS')
        # Check the actual GPRs after flushing scratch-register changes.
        batch = w._Batch()
        h._queue_restore(batch)
        h._run(batch)
        batch = w._Batch()
        for n in range(1, 32):
            batch.cmd(w._cmd_read(n)).r(w.DATA0)
        assert h._run(batch) == original[1:]
        print('31 hardware GPRs preserved after memory/CSR/FPR access: PASS')
        old_pc = h.pc()
        h.step()
        assert h.halt_cause() == 4
        assert not h.read_csr(w.CSR_DCSR) & w.DCSR_STEP
        print('single step: %08x -> %08x, cause=4, step bit cleared' % (old_pc, h.pc()))
    finally:
        try:
            if not was_halted:
                h.resume()
            else:
                batch = w._Batch()
                h._queue_restore(batch)
                h._run(batch)
        finally:
            dap.close()


if __name__ == '__main__':
    main()
