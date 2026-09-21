# SPDX-License-Identifier: GPL-2.0-only
"""Fault regressions for Flash recovery, software traps, CFI and snapshots."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gdbserver import Server, unescape
from ws63break import SoftwareBreakpoints
from ws63dbg import DebugError, CSR_DCSR
from ws63diagnose import FrozenHart
from ws63elf import cfi_rows, reglist, unwind_states
from ws63flash import Flash, SECTOR, validate_chunks
from ws63rtos import LiteOS


class MemoryHart:
    def __init__(self):
        self.mem = {}
        self.csrs = {CSR_DCSR: 0}
        self.dap = Mock()
        self.resume = Mock()
        self.fail_write = False

    def halted(self): return True
    def read_mem(self, a, n): return bytes(self.mem.get(a+i, 0xff) for i in range(n))
    def write_mem(self, a, data):
        if self.fail_write: raise DebugError('write fault')
        self.mem.update({a+i: b for i, b in enumerate(data)})
    def read_csr(self, n): return self.csrs.get(n, 0)
    def write_csr(self, n, v): self.csrs[n] = v
    def sync_code(self): pass


class ModelFlash(Flash):
    def __init__(self, root):
        super().__init__(MemoryHart(), root)
        self.sr = (0x3c, 0x42)
        self.writes = []
        self.fail = False

    def identify(self): return 0x1640c8
    def status(self): return self.sr
    def set_status(self, a, b): self.sr = (a, b)
    def ready(self): pass
    @contextmanager
    def controller(self): yield
    def write_sector(self, address, data):
        # Every sector must have a durable backup before the first erase.
        manifest = json.loads((Path(self.last_journal)/'journal.json').read_text())
        for item in manifest['sectors']:
            assert (Path(self.last_journal)/item['file']).exists()
        self.h.write_mem(address, b'\xff' * SECTOR)
        if self.fail: raise DebugError('injected failure after erase')
        self.h.write_mem(address, data)
        self.writes.append(address)


class FlashTests(unittest.TestCase):
    def test_borrowed_controller_restores_prepared_command_and_write_latch(self):
        h=MemoryHart()
        f=Flash(h)
        registers={0x240:0,0x300:0x82,0x308:2,0x30c:0x123456}
        original=dict(registers)
        buffer=list(range(16))
        sr=[2]
        f.rd=lambda off:registers[off]
        f.wr=lambda off,v:registers.__setitem__(off,v)
        f.ready=lambda:None
        f.idle=lambda:None
        h.dap.bus_read_words.side_effect=lambda a,n:list(buffer)
        h.dap.bus_write_words.side_effect=lambda a,data:buffer.__setitem__(slice(None),data)
        def command(opcode,address=None,data=b'',read=0):
            registers[0x308]=opcode
            registers[0x30c]=0
            buffer[:]=[0]*16
            if opcode==5:return bytes(sr)
            if opcode==6:sr[0]=2
            else:sr[0]=0
            return b''
        f._command=command
        f.command(2,0x400,data=b'written')
        self.assertEqual(registers,original)
        self.assertEqual(buffer,list(range(16)))
        self.assertEqual(sr,[2])

    def test_partial_write_preserves_sector_and_restores_status(self):
        with tempfile.TemporaryDirectory() as root:
            f = ModelFlash(root)
            old = bytes(range(256)) * 16
            f.h.write_mem(0x230000, old)
            result = f.apply([(0x230123, b'test')])
            self.assertEqual(f.read(0x230000, SECTOR), old[:0x123]+b'test'+old[0x127:])
            self.assertEqual(f.sr, (0x3c, 0x42))
            f.restore(result['journal'])
            self.assertEqual(f.read(0x230000, SECTOR), old)
            self.assertFalse(f.unsafe)

    def test_failure_keeps_recovery_data_and_blocks_continue(self):
        with tempfile.TemporaryDirectory() as root:
            f = ModelFlash(root)
            f.h.write_mem(0x230000, b'A'*SECTOR)
            f.fail = True
            with self.assertRaisesRegex(DebugError, 'after erase'):
                f.apply([(0x230123, b'B')])
            self.assertTrue(f.unsafe)
            self.assertEqual(f.sr, (0x3c, 0x42))
            server = Server(f.h, flash=f)
            self.assertEqual(server.handle('c'), 'E01')
            f.h.resume.assert_not_called()
            journal = f.last_journal
            f.fail = False
            f.restore(journal)
            self.assertEqual(f.read(0x230000, SECTOR), b'A'*SECTOR)

    def test_corrupt_journal_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as root:
            f = ModelFlash(root)
            result = f.apply([(0x230000, b'test')])
            (Path(result['journal'])/'00230000.bin').write_bytes(b'X'*SECTOR)
            f.writes.clear()
            with self.assertRaisesRegex(DebugError, 'corrupt'):
                f.restore(result['journal'])
            self.assertFalse(f.writes)

    def test_unchanged_writes_are_skipped_unless_forced(self):
        with tempfile.TemporaryDirectory() as root:
            f = ModelFlash(root)
            self.assertEqual(f.apply([(0x230000, b'\xff')])['sectors'], 0)
            self.assertEqual(f.apply([(0x230000, b'\xff')], force=True)['sectors'], 1)

    def test_ranges_and_overlap_are_validated(self):
        for chunks in ([], [(0x1fffff, b'X')], [(0x5fffff, b'XX')],
                       [(0x230000, b'XX'), (0x230001, b'Y')]):
            with self.subTest(chunks=chunks), self.assertRaises(DebugError):
                validate_chunks(chunks)


class ExtensionRSPTests(unittest.TestCase):
    def test_gdb_register_restore_invalidates_task_backtrace_cache(self):
        r=Mock()
        r.live_selected.return_value=True
        s=Server(Mock(),rtos=r)
        self.assertEqual(s.handle('G'+'00000000'*33),'OK')
        r.invalidate.assert_called_once_with()

    def test_reset_vector_does_not_expose_stale_task_array(self):
        r=LiteOS.__new__(LiteOS)
        r.valid=False;r.h=Mock();r.image=Mock()
        r.h.halted.return_value=True;r.h.pc.return_value=0x100000
        r.refresh()
        self.assertEqual(r.current,1)
        self.assertEqual(r.tasks,{})
        r.image.check_target.assert_not_called()
        r.h.read_mem.assert_not_called()

    def test_binary_flash_staging_preserves_escape_bytes_and_erases_gaps(self):
        f = Mock(unsafe=False)
        f.apply.return_value = {'sectors': 1}
        s = Server(MemoryHart(), flash=f)
        self.assertEqual(s.handle('vFlashWrite:230000:bad'), 'E01')
        self.assertEqual(s.handle('vFlashErase:230000,1000'), 'OK')
        wire = ''.join('}'+chr(c^0x20) for c in b'#$}*')
        self.assertEqual(s.handle('vFlashWrite:230002:'+wire), 'OK')
        f.apply.assert_not_called()
        self.assertEqual(s.handle('vFlashDone'), 'OK')
        payload = f.apply.call_args.args[0][0][1]
        self.assertEqual(payload[:8], b'\xff\xff#$}*\xff\xff')
        self.assertEqual(len(payload), SECTOR)
        self.assertTrue(s.needs_reset)
        self.assertFalse(s.flash_writes)

    def test_overlapping_or_unaligned_erase_rejected(self):
        s = Server(MemoryHart(), flash=Mock())
        self.assertEqual(s.handle('vFlashErase:230001,1000'), 'E01')
        self.assertEqual(s.handle('vFlashErase:230000,1000'), 'OK')
        self.assertEqual(s.handle('vFlashErase:230000,1000'), 'E01')

    def test_unchanged_gdb_load_still_requires_reset(self):
        f = Mock(unsafe=False)
        f.apply.return_value = {'sectors': 0}
        s = Server(MemoryHart(), flash=f)
        s.handle('vFlashErase:230000,1000')
        self.assertEqual(s.handle('vFlashDone'), 'OK')
        self.assertTrue(s.needs_reset)
        self.assertEqual(s.handle('c230000'), 'E01')

    def test_offline_mutations_never_reach_hardware(self):
        h = Mock()
        s = Server(h, readonly=True)
        for packet in ('c1234','s1234','M100,1:00','X100,1:A','P20=00100000','Z0,a00000,2'):
            try:
                result = s.handle(packet)
                self.assertIn(result, ('E01','E14'))
            except DebugError:
                pass
        self.assertFalse(h.mock_calls)

    def test_noncurrent_registers_are_unknown_and_not_writable(self):
        r = Mock(selected=4)
        r.live_selected.return_value = False
        r.tasks = {4: Mock(registers={2:0xa12340,32:0xa01234})}
        s = Server(Mock(), rtos=r)
        self.assertEqual(s.read_reg(1), 'xxxxxxxx')
        self.assertEqual(s.read_reg(32), '3412a000')
        with self.assertRaises(DebugError): s.write_reg(32, 0)
        self.assertFalse(s.h.mock_calls)

    def test_truncated_binary_escape_rejected(self):
        with self.assertRaises(ValueError): unescape('abc}')


class SoftwareTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHart()
        self.a = 0xa01000
        self.h.write_mem(self.a, b'ABCD')
        self.b = SoftwareBreakpoints(self.h)

    def test_shadow_displaced_step_and_remove_restore_original(self):
        self.b.insert(self.a, 4)
        self.assertEqual(self.b.read(self.a, 4), b'ABCD')
        self.assertEqual(self.h.read_mem(self.a, 4), b'\x73\0\x10\0')
        with self.b.displaced(self.a):
            self.assertEqual(self.h.read_mem(self.a, 4), b'ABCD')
        self.b.remove(self.a)
        self.assertEqual(self.h.read_mem(self.a, 4), b'ABCD')
        self.assertEqual(self.h.read_csr(CSR_DCSR), 0)

    def test_failed_insert_does_not_leave_entry_or_dcsr_changed(self):
        self.h.fail_write = True
        with self.assertRaises(DebugError): self.b.insert(self.a, 4)
        self.assertFalse(self.b.entries)
        self.assertEqual(self.h.read_csr(CSR_DCSR), 0)

    def test_write_over_breakpoint_updates_original_and_keeps_trap(self):
        self.b.insert(self.a, 4)
        self.b.write(self.a+1, b'XY')
        self.assertEqual(self.b.read(self.a, 4), b'AXYD')
        self.assertEqual(self.h.read_mem(self.a, 4), b'\x73\0\x10\0')
        self.b.clear()
        self.assertEqual(self.h.read_mem(self.a, 4), b'AXYD')

    def test_self_modifying_code_is_not_overwritten_on_cleanup(self):
        self.b.insert(self.a, 4)
        self.h.write_mem(self.a, b'NEW!')
        with self.assertRaises(DebugError): self.b.clear()
        self.assertTrue(self.b.entries)
        self.assertEqual(self.h.read_mem(self.a, 4), b'NEW!')


class UnwindTests(unittest.TestCase):
    def test_riscv_saved_register_range_is_not_numeric_contiguous(self):
        self.assertEqual(reglist('ra,s0-s2'), [1,8,9,18])

    def test_branch_after_epilogue_keeps_the_correct_frame(self):
        insns = {0:(2,'push','{ra,s0-s2}, -16'),
                 2:(2,'beqz','a0,8 <body>'),
                 4:(2,'popret','{ra,s0-s2}, 16'),
                 8:(4,'jal','100 <callee>'),
                 12:(2,'popret','{ra,s0-s2}, 16')}
        states = unwind_states(0,14,insns)
        self.assertEqual(states[8][0], -16)
        self.assertEqual(states[8][1], {1:-4,8:-8,9:-12,18:-16})
        self.assertIsNotNone(cfi_rows(0,14,states))

    def test_unknown_stack_adjustment_is_rejected(self):
        self.assertFalse(unwind_states(0,8,{0:(4,'add','sp,sp,a0'),4:(4,'ret','')}))

    def test_modified_return_address_is_not_invented_as_saved(self):
        insns = {0:(4,'addi','sp,sp,-16'),4:(4,'li','ra,123'),
                 8:(4,'sw','ra,12(sp)'),12:(4,'ret','')}
        states = unwind_states(0,16,insns)
        self.assertNotIn(1, states[12][1])
        self.assertIsNone(cfi_rows(0,16,states))


class SnapshotTests(unittest.TestCase):
    def test_missing_memory_and_tampering_are_errors(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            data = b'abcd'
            (p/'ram.bin').write_bytes(data)
            metadata = dict(version=1,registers={'32':0xa00000},segments=[dict(
                address=0xa00000,size=4,file='ram.bin',sha256=hashlib.sha256(data).hexdigest())])
            (p/'snapshot.json').write_text(json.dumps(metadata))
            h = FrozenHart(p)
            self.assertEqual(h.read_mem(0xa00001,2), b'bc')
            with self.assertRaises(DebugError): h.read_mem(0xa00002,4)
            (p/'ram.bin').write_bytes(b'bad!')
            with self.assertRaisesRegex(DebugError,'corrupt'): FrozenHart(p)


if __name__ == '__main__': unittest.main()
