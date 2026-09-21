# SPDX-License-Identifier: GPL-2.0-only
"""Watchpoint range, instruction and failure regressions without hardware."""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gdbserver import Server
from ws63dbg import DebugError, DMSTATUS_ALLHALTED
from ws63watch import Access, Watchpoints, decode_access, range_blocks


class DecodeTests(unittest.TestCase):
    def test_standard_and_ws63_scalar_instructions(self):
        registers = lambda n: 0x1000*n
        cases = [
            ('9c23', Access(0xf000, 1, 'load')),  # SDK gpio lbu a5,0(a5)
            ('1ce3', Access(0xe000, 4, 'store')),  # c.fsw fa5,0(a4)
            ('2225', Access(0xa00a, 2, 'load')),  # WS63 lhu s0,10(a0)
            ('2030', Access(0x8003, 1, 'load')),
            ('02b0', Access(0x8020, 2, 'store')),
            ('8240', Access(0x2000, 4, 'load')),  # c.lwsp ra,0(sp)
            ('02c4', Access(0x2008, 4, 'store')),
            ('03a5c5ff', Access(0xb000-4, 4, 'load')),  # lw a0,-4(a1)
            ('23aed5fe', Access(0xb000-4, 4, 'store')),
            ('07a50500', Access(0xb000, 4, 'load')),  # flw fa0,0(a1)
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(decode_access(bytes.fromhex(code), registers), expected)

    def test_unknown_long_reserved_and_truncated_are_not_guessed(self):
        for raw in ('', '03', '03a5', '1f0000000000', '3880', '0100', '0240', '2fa50500'):
            self.assertIsNone(decode_access(bytes.fromhex(raw), lambda n: 0x1000))

    def test_misaligned_access_is_not_used_to_auto_resume(self):
        self.assertIsNone(decode_access(bytes.fromhex('03a50500'), lambda n: 0x1001))

    def test_zero_base_and_negative_offset_wrap_at_rv32(self):
        self.assertEqual(decode_access(bytes.fromhex('0345f0ff'), lambda n: 42),
                         Access(0xffffffff, 1, 'load'))


class RangeTests(unittest.TestCase):
    def test_arbitrary_ranges_cover_all_aligned_scalar_starts(self):
        for address in range(0x1000, 0x1010):
            for length in (1, 2, 3, 4, 8, 13, 32, 257):
                blocks = range_blocks(address, length)
                self.assertEqual(blocks[0][0], address & ~3)
                self.assertEqual(sum(n for _, n in blocks), ((address+length+3)&~3)-(address&~3))
                for a, n in blocks:
                    self.assertGreaterEqual(n, 4)
                    self.assertEqual(n & (n-1), 0)
                    self.assertEqual(a % n, 0)
                for width in (1, 2, 4):
                    for start in range(address-3, address+length):
                        if start % width == 0 and start+width > address:
                            self.assertTrue(any(a <= start < a+n for a, n in blocks))

    def test_address_space_edges_and_invalid_ranges(self):
        self.assertEqual(range_blocks(0xffffffff, 1), [(0xfffffffc, 4)])
        self.assertEqual(range_blocks(0, 1 << 32), [(0, 1 << 32)])
        for address, length in ((-1, 1), (0, 0), (0xffffffff, 2)):
            with self.assertRaises(ValueError):
                range_blocks(address, length)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.h = Mock()
        self.h.count_triggers.return_value = 8
        self.s = Server(self.h)
        self.h.pc.return_value = 0x300000
        self.h.halt_cause.return_value = 2
        self.h.read_mem.return_value = bytes.fromhex('9c23')
        self.h.read_gpr.return_value = 0xa00000

    def test_slot_exhaustion_does_not_partially_program_hardware(self):
        self.s.breakpoints = {0x300000+i*4:i for i in range(7)}
        self.assertEqual(self.s.handle('Z3,a00003,6'), 'E0E')  # two aligned blocks
        self.h.set_trigger.assert_not_called()
        self.assertFalse(self.s.watchpoints.entries)

    def test_duplicate_and_length_specific_removal(self):
        self.assertEqual(self.s.handle('Z3,a00000,4'), 'OK')
        self.assertEqual(self.s.handle('Z3,a00000,4'), 'OK')
        self.h.set_trigger.assert_called_once()
        self.assertEqual(self.s.handle('z3,a00000,1'), 'OK')
        self.assertEqual(self.s.watchpoints.slots(), [0])
        self.s.handle('z3,a00000,4')
        self.assertFalse(self.s.watchpoints.slots())

    def test_failed_setup_rolls_back_all_reserved_slots(self):
        self.h.set_trigger.side_effect = [None, DebugError('write failed')]
        with self.assertRaises(DebugError):
            self.s.handle('Z3,a00003,6')
        self.assertEqual([c.args[0] for c in self.h.clear_trigger.call_args_list], [0, 1])
        self.assertFalse(self.s.watchpoints.entries)
        self.assertFalse(self.s.watchpoints.unsafe)

    def test_failed_rollback_keeps_ownership_and_blocks_execution(self):
        self.h.set_trigger.side_effect = DebugError('write failed')
        self.h.clear_trigger.side_effect = DebugError('clear failed')
        with self.assertRaises(DebugError):
            self.s.handle('Z3,a00003,6')
        self.assertEqual(self.s.watchpoints.slots(), [0, 1])
        self.assertTrue(self.s.watchpoints.unsafe)
        self.assertEqual(self.s.handle('c'), 'E01')
        self.assertEqual(self.s.handle('s'), 'E01')
        self.h.resume.assert_not_called()
        self.h.clear_trigger.side_effect = None
        self.s.handle('z3,a00003,6')
        self.assertFalse(self.s.watchpoints.unsafe)

    def test_failed_disable_does_not_step_or_resume(self):
        self.s.handle('Z3,a00000,4')
        self.h.clear_trigger.side_effect = DebugError('disconnected')
        with self.assertRaises(DebugError):
            self.s.step_over()
        self.assertTrue(self.s.watchpoints.unsafe)
        self.h.step.assert_not_called()
        self.h.resume.assert_not_called()

    def test_partial_overlap_reports_an_address_inside_watched_range(self):
        self.s.handle('Z3,a00001,1')
        self.h.read_mem.side_effect = [bytes.fromhex('03a5'), bytes.fromhex('0500')]
        self.assertEqual(self.s.stop_reply(), 'T05rwatch:a00001;')

    def test_load_and_store_watchpoints_are_distinguished(self):
        self.s.handle('Z2,a00000,4')
        self.s.handle('Z3,a00000,4')
        self.assertEqual(self.s.stop_reply(), 'T05rwatch:a00000;')
        self.h.read_mem.return_value = bytes.fromhex('9ca3')  # WS63 sb a5,0(a5)
        self.assertEqual(self.s.stop_reply(), 'T05watch:a00000;')

    def test_filter_steps_only_proven_outside_access(self):
        self.s.handle('Z3,a00001,1')
        self.s.step_over = Mock(side_effect=lambda: setattr(self.h.halt_cause, 'return_value', 4))
        self.assertTrue(self.s.skip_watch_guard())
        self.s.step_over.assert_called_once()
        self.h.resume.assert_called_once()
        self.assertEqual(self.s.watch_filtered, 1)

    def test_filter_does_not_resume_an_unexpected_step_stop(self):
        self.s.handle('Z3,a00001,1')
        self.s.step_over = Mock(side_effect=lambda: setattr(self.h.halt_cause, 'return_value', 3))
        self.assertFalse(self.s.skip_watch_guard())
        self.h.resume.assert_not_called()
        self.assertEqual(self.s.watch_filtered, 0)

    def test_failed_reenable_preserves_ownership_and_blocks_resume(self):
        self.s.handle('Z3,a00003,6')
        self.h.set_trigger.side_effect = [None, DebugError('disconnected')]
        with self.assertRaises(DebugError):
            self.s.step_over()
        self.assertEqual(self.s.watchpoints.slots(), [0, 1])
        self.assertTrue(self.s.watchpoints.unsafe)
        self.assertEqual(self.s.handle('c'), 'E01')
        self.h.resume.assert_not_called()

    def test_reset_restores_all_range_blocks_and_forgets_previous_stop(self):
        self.s.handle('Z3,a00003,6')
        self.s.handle('Z2,a00010,8')
        expected = self.h.set_trigger.call_args_list[:]
        self.h.set_trigger.reset_mock()
        self.s.watch_stop_pc = 0x300000
        self.s.after_reset(resume=False)
        self.assertEqual(self.h.set_trigger.call_args_list, expected)
        self.assertIsNone(self.s.watch_stop_pc)
        self.h.resume.assert_not_called()

    def test_unknown_instruction_and_read_error_stay_halted(self):
        self.s.handle('Z3,a00001,1')
        for result in (bytes.fromhex('3880'), DebugError('unmapped PC')):
            self.h.read_mem.side_effect = result if isinstance(result, Exception) else None
            self.h.read_mem.return_value = result
            self.assertFalse(self.s.skip_watch_guard())
            self.assertEqual(self.s.stop_reply(), 'S05')
            self.h.resume.assert_not_called()
            self.h.step.assert_not_called()

    def test_execution_breakpoint_wins_over_guard_filter(self):
        self.s.handle('Z3,a00001,1')
        self.s.breakpoints[0x300000] = 1
        self.assertFalse(self.s.skip_watch_guard())
        self.assertEqual(self.s.stop_reply(), 'T05hwbreak:;')
        self.h.read_mem.assert_not_called()

    def test_continue_after_data_stop_steps_once(self):
        self.s.watch_stop_pc = 0x300000
        self.s.step_over = Mock()
        self.s.stop_reply = Mock(return_value='S05')
        self.h.halt_cause.return_value = 4
        self.h.status.return_value = DMSTATUS_ALLHALTED
        with patch('gdbserver.select.select', return_value=([], [], [])):
            self.assertEqual(self.s.do_continue(), 'S05')
        self.s.step_over.assert_called_once()
        self.h.resume.assert_called_once()

    def test_detach_restores_triggers_before_resume(self):
        self.s.handle('Z3,a00003,6')
        self.assertEqual(self.s.handle('D'), 'OK')
        self.assertFalse(self.s.watchpoints.entries)
        self.h.resume.assert_called_once()


if __name__ == '__main__':
    unittest.main()
