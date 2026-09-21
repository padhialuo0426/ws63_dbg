# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""Offline fault regressions; no probe or target is opened."""
import collections
import importlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get('WS63_TOOLS', str(Path(__file__).resolve().parents[1])))
import ws63dbg as w
import gdbserver as g
import cmsisdap as c
import ws63dump


class DebugModule:
    """Small DM execution model that records actual program-buffer loads/stores."""
    def __init__(self):
        self.regs = collections.defaultdict(int)
        self.gpr = [0] * 32
        self.loads = []
        self.stores = []
        self.tar = 0
        self.fail_auto = False

    def select(self, ap):
        pass

    def execute(self):
        cmd = self.regs[w.COMMAND]
        n = cmd & 31
        if cmd & (1 << 16):
            self.gpr[n] = self.regs[w.DATA0]
        else:
            self.regs[w.DATA0] = self.gpr[n]
        if cmd & (1 << 18):
            for i in range(3):
                insn = self.regs[w.PROGBUF0 + i * 4]
                if insn == w.EBREAK:
                    break
                if insn == 0x00042483:
                    self.loads.append(self.gpr[8])
                    self.gpr[9] = self.gpr[8] ^ 0x55AA
                elif insn == 0x00942023:
                    self.stores.append((self.gpr[8], self.gpr[9]))
                elif insn == 0x00440413:
                    self.gpr[8] += 4
                else:
                    raise AssertionError('unexpected instruction %x' % insn)

    def read(self, off):
        val = self.regs[off]
        if off == w.DATA0 and self.regs[w.ABSTRACTAUTO]:
            self.execute()
        return val

    def write(self, off, value):
        self.regs[off] = value
        if off == w.COMMAND:
            self.execute()
        if off == w.ABSTRACTAUTO and value and self.fail_auto:
            raise w.DebugError('injected transport failure after autoexec enabled')
        if off == w.DATA0 and self.regs[w.ABSTRACTAUTO]:
            self.execute()

    def transfer(self, ops):
        out = []
        for reg, ap, val in ops:
            if reg == 1:
                self.tar = val
            elif val is None:
                out.append(self.read(self.tar))
            else:
                self.write(self.tar, val)
        return out

    def ap_read32(self, ap, off):
        return self.read(off)

    def ap_write32(self, ap, off, value):
        self.write(off, value)

    def ap_read_repeat(self, ap, off, n):
        return [self.read(off) for _ in range(n)]


def model_hart():
    h = w.Hart.__new__(w.Hart)
    h.dap = DebugModule()
    h.cache = [0] * 32
    h.clobbered = set()
    h.progbuf = [None] * 3
    return h


class MemoryTests(unittest.TestCase):
    def test_read_does_not_touch_word_beyond_range(self):
        for count in (1, 2, 3, 16, 257):
            with self.subTest(count=count):
                h = model_hart()
                addresses = list(range(0xA00100, 0xA00100 + 4 * count, 4))
                self.assertEqual(h._pb_read_words(0xA00100, count), [a ^ 0x55AA for a in addresses])
                self.assertEqual(h.dap.loads, addresses)
                self.assertEqual(h.dap.regs[w.ABSTRACTAUTO], 0)

    def test_read_failure_disables_autoexec(self):
        h = model_hart()
        h.dap.fail_auto = True
        with self.assertRaises(w.DebugError):
            h._pb_read_words(0xA00000, 4)
        self.assertEqual(h.dap.regs[w.ABSTRACTAUTO], 0)

    def test_write_failure_disables_autoexec(self):
        h = model_hart()
        h.dap.fail_auto = True
        with self.assertRaises(w.DebugError):
            h._pb_write_words(0xA00000, [1, 2])
        self.assertEqual(h.dap.regs[w.ABSTRACTAUTO], 0)

    def test_write_exact_range(self):
        h = model_hart()
        h._pb_write_words(0xA00000, [0x12, 0x34, 0x56])
        self.assertEqual(h.dap.stores, [(0xA00000, 0x12), (0xA00004, 0x34), (0xA00008, 0x56)])

    def test_empty_memory_requests_do_not_access_target(self):
        h = model_hart()
        h.dap = Mock()
        self.assertEqual(h.read_mem(0xA00001, 0), b'')
        h.write_mem(0xA00001, b'')
        self.assertFalse(h.dap.mock_calls)

    def test_memory_range_is_validated_before_access(self):
        h = model_hart()
        h.dap = Mock()
        for addr, size in [(-1, 4), (0, -1), (0xFFFFFFFF, 2)]:
            with self.subTest(addr=addr, size=size), self.assertRaises(w.DebugError):
                h.read_mem(addr, size)
        self.assertFalse(h.dap.mock_calls)

    def test_busy_timeout_is_an_error(self):
        h = model_hart()
        h.dap.regs[w.ABSTRACTCS] = 1 << 12
        with self.assertRaisesRegex(w.DebugError, 'busy'):
            h._run(w._Batch())

    def test_resume_waits_for_ack_even_if_still_halted(self):
        h = model_hart()
        h.status = Mock(side_effect=[w.DMSTATUS_ALLHALTED, w.DMSTATUS_ALLRESUMEACK])
        h.resume()
        self.assertEqual(h.status.call_count, 2)

    def test_resume_missing_ack_is_an_error(self):
        h = model_hart()
        h.status = Mock(return_value=w.DMSTATUS_ALLHALTED)
        with self.assertRaisesRegex(w.DebugError, 'resume'):
            h.resume()
        self.assertEqual(h.dap.regs[w.DMCONTROL], w.DMCONTROL_DMACTIVE)

    def test_failed_ap_init_is_retried(self):
        dap = w.DAP()
        dap.wr = Mock(side_effect=[None, w.DebugError('fault')])
        with self.assertRaises(w.DebugError):
            dap.select(1)
        dap.wr = Mock()
        dap.select(1)
        self.assertEqual(dap.wr.call_count, 2)

    def test_invalid_csr_has_no_side_effect(self):
        h = model_hart()
        with self.assertRaises(w.DebugError):
            h.write_csr(0x1000, 0)
        self.assertFalse(h.clobbered)


class Stream:
    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = bytearray()

    def recv(self, n):
        # TCP may split the two checksum digits into separate reads.
        out = bytes(self.data[:1])
        del self.data[:1]
        return out

    def sendall(self, data):
        self.sent += data


def packet(data):
    return b'$' + data + b'#' + ('%02x' % (sum(data) & 255)).encode()


class RSPTests(unittest.TestCase):
    def setUp(self):
        self.h = Mock()
        self.s = g.Server(self.h)

    def test_corrupt_packet_is_rejected_then_retransmitted(self):
        self.s.sock = Stream(b'$M100,1:01#00' + packet(b'qC'))
        self.assertEqual(self.s.recv_packet(), 'qC')
        self.assertEqual(self.s.sock.sent, b'-+')
        self.assertFalse(self.h.mock_calls)

    def test_fragmented_checksum_is_fully_consumed(self):
        self.s.sock = Stream(packet(b'qC') + packet(b'?'))
        self.assertEqual(self.s.recv_packet(), 'qC')
        self.assertEqual(bytes(self.s.sock.data), packet(b'?'))

    def test_incomplete_checksum_is_not_acknowledged(self):
        self.s.sock = Stream(packet(b'qC')[:-1])
        self.assertIsNone(self.s.recv_packet())
        self.assertEqual(self.s.sock.sent, b'')

    def test_empty_packet_is_unsupported(self):
        self.assertEqual(self.s.handle(''), '')

    def test_malformed_requests_leave_target_untouched(self):
        for request in ('M100,2:aa', 'Mbad', 'P8=01', 'G00', 'Zbad', 'm100,zz'):
            with self.subTest(request=request):
                self.assertEqual(self.s.handle(request), 'E01')
        self.assertFalse(self.h.mock_calls)

    def test_oversized_memory_read_is_rejected(self):
        self.assertEqual(self.s.handle('m100,10000000'), 'E14')
        self.h.read_mem.assert_not_called()

    def test_multibyte_watchpoint_programs_real_range(self):
        self.h.count_triggers.return_value = 8
        self.assertEqual(self.s.handle('Z3,a00000,4'), 'OK')
        self.h.set_trigger.assert_called_once_with(0, 0xA00000, 'load', 4)

    def test_second_watchpoint_hit_is_decoded_without_hit_bits(self):
        self.h.count_triggers.return_value = 8
        self.s.handle('Z3,a00000,1')
        self.s.handle('Z3,a00001,1')
        self.h.halt_cause.return_value = 2
        self.h.pc.return_value = 0x300000
        self.h.read_mem.return_value = bytes.fromhex('9c23')  # WS63 lbu a5,0(a5)
        self.h.read_gpr.return_value = 0xA00001
        self.assertEqual(self.s.stop_reply(), 'T05rwatch:a00001;')

    def test_ws63_watchpoint_without_hit_bit(self):
        self.h.count_triggers.return_value = 8
        self.s.handle('Z3,a00000,1')
        self.h.halt_cause.return_value = 2
        self.h.pc.return_value = 0x300000
        self.h.read_mem.return_value = bytes.fromhex('9c23')
        self.h.read_gpr.return_value = 0xA00000
        self.assertEqual(self.s.stop_reply(), 'T05rwatch:a00000;')

    def test_execution_breakpoint_is_not_misreported_as_watchpoint(self):
        self.h.count_triggers.return_value = 8
        self.s.handle('Z3,a00000,1')
        self.s.breakpoints = {0x300000: 1}
        self.h.halt_cause.return_value = 2
        self.h.trigger_hit.return_value = False
        self.h.pc.return_value = 0x300000
        self.assertEqual(self.s.stop_reply(), 'T05hwbreak:;')

    def test_float_register_write_routes_to_hart(self):
        self.assertEqual(self.s.handle('P21=0000803f'), 'OK')
        self.h.write_fpr.assert_called_once_with(0, 0x3F800000)


class ProbeTests(unittest.TestCase):
    def test_single_transfer_checks_executed_count(self):
        dap = c.CMSISDAP.__new__(c.CMSISDAP)
        dap.cmd = Mock(return_value=b'\x05\x00\x01' + bytes(61))
        for operation in (lambda: dap.rd(0, 0), lambda: dap.wr(0, 0, 123)):
            with self.assertRaises(w.DebugError):
                operation()

    def test_truncated_transfer_is_debug_error(self):
        dap = c.CMSISDAP.__new__(c.CMSISDAP)
        dap.cmd = Mock(return_value=b'\x05')
        with self.assertRaises(w.DebugError):
            dap.rd(0, 0)

    def test_value_mismatch_is_an_error(self):
        dap = c.CMSISDAP.__new__(c.CMSISDAP)
        dap.clear_errors = Mock()
        with self.assertRaises(w.DebugError):
            dap._check(0x11, 'test')


class DumpTests(unittest.TestCase):
    def test_preserves_registers_of_previously_halted_target(self):
        h = model_hart()
        h.halted = Mock(return_value=True)
        h.halt = Mock()
        dap = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(ws63dump.ws63dbg, 'connect', return_value=(dap, h)), \
             patch.object(sys, 'argv', ['ws63dump.py', '0xa00000', '4', str(Path(tmp) / 'out.bin'), '--halt']):
            ws63dump.main()
        self.assertEqual(h.dap.gpr[8:10], [0, 0])
        self.assertFalse(h.clobbered)

    def test_preserves_preexisting_halt(self):
        dap, h = Mock(), Mock()
        h.halted.return_value = True
        h.read_mem.return_value = b'abcd'
        with tempfile.TemporaryDirectory() as tmp, patch.object(ws63dump.ws63dbg, 'connect', return_value=(dap, h)), \
             patch.object(sys, 'argv', ['ws63dump.py', '0xa00000', '4', str(Path(tmp) / 'out.bin'), '--halt']):
            ws63dump.main()
        h.resume.assert_not_called()
        dap.close.assert_called_once()

    def test_close_even_if_resume_fails(self):
        dap, h = Mock(), Mock()
        h.halted.return_value = False
        h.read_mem.return_value = b'abcd'
        h.resume.side_effect = w.DebugError('link lost')
        with tempfile.TemporaryDirectory() as tmp, patch.object(ws63dump.ws63dbg, 'connect', return_value=(dap, h)), \
             patch.object(sys, 'argv', ['ws63dump.py', '0xa00000', '4', str(Path(tmp) / 'out.bin'), '--halt']):
            with self.assertRaises(w.DebugError):
                ws63dump.main()
        dap.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
