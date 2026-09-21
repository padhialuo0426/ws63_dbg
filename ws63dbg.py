#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""WS63 SWD debug library: raw CoreSight DP/AP access through a CMSIS-DAP probe.

WS63 debug topology:
    SW-DP (DPIDR 0x5BA02477)
      AP0: APB-AP -> RISC-V Debug Module (spec 0.13) at offset 0 -> RV32 hart
      AP1: AHB-AP -> system bus (ROM / flash XIP / SRAM / peripherals)

The DM supports abstract access to GPRs and a 3-entry program buffer (no impebreak),
but not abstract access to CSRs, so CSRs are accessed through the program buffer.
This backend handles the CoreSight transport and WS63 cache/debug quirks.

The SWD pins (GPIO_13 = SWDIO, GPIO_14 = SWCLK, pin mode 4) are GPIOs after reset;
the firmware must switch them to the debug function first (see docs/02-enable-swd.md for the flashboot changes).
"""
import struct
import time

DP, AP = 0, 1
AP_DM, AP_MEM = 0, 1

# Debug Module register byte offsets (DM register index * 4)
DATA0 = 0x10
DMCONTROL = 0x40
DMSTATUS = 0x44
ABSTRACTCS = 0x58
COMMAND = 0x5C
ABSTRACTAUTO = 0x60
PROGBUF0 = 0x80

# Read-only regions (coherent over the AHB-AP); everything else may sit in the D-cache
ROM_BASE, ROM_END = 0x100000, 0x14C000
FLASH_BASE, FLASH_END = 0x200000, 0x600000

DMCONTROL_DMACTIVE = 1 << 0
DMCONTROL_ACKHAVERESET = 1 << 28
DMCONTROL_RESUMEREQ = 1 << 30
DMCONTROL_HALTREQ = 1 << 31
DMSTATUS_ALLHALTED = 1 << 9
DMSTATUS_ALLRESUMEACK = 1 << 17
DMSTATUS_ANYHAVERESET = 1 << 18
DMCONTROL_NDMRESET = 1 << 1

# CSRs
CSR_TSELECT, CSR_TDATA1, CSR_TDATA2 = 0x7A0, 0x7A1, 0x7A2
CSR_DCSR, CSR_DPC = 0x7B0, 0x7B1
DCSR_STEP = 1 << 2

# Instructions used in the program buffer (s0 = x8 is the scratch register)
EBREAK = 0x00100073
S0 = 8

# mcontrol (type 2) trigger: dmode | action=enter debug mode | M | U
MCONTROL_BASE = (2 << 28) | (1 << 27) | (1 << 12) | (1 << 6) | (1 << 3)
MCONTROL_EXECUTE, MCONTROL_STORE, MCONTROL_LOAD = 1 << 2, 1 << 1, 1 << 0
MCONTROL_HIT = 1 << 20

class DebugError(Exception):
    pass


class DAP:
    """ADIv5 DP/AP layer on top of a probe backend (see cmsisdap.CMSISDAP).

    A backend implements rd(reg, apndp), wr(reg, apndp, val), clear_errors(), reconnect()
    and close(). reg is the register index A[3:2] (0..3). It may override transfer(),
    rd_repeat() and wr_repeat() to batch many accesses into a single USB transaction.
    """

    name = "?"

    def __init__(self):
        self.cur_ap = None

    def transfer(self, ops):
        """Run a sequence of (reg, apndp, value) accesses; value None means read.
        Returns the read values in order. Backends may send the whole list at once."""
        out = []
        for reg, apndp, val in ops:
            if val is None:
                out.append(self.rd(reg, apndp))
            else:
                self.wr(reg, apndp, val)
        return out

    def rd_repeat(self, reg, apndp, n):
        return [self.rd(reg, apndp) for _ in range(n)]

    def wr_repeat(self, reg, apndp, values):
        for v in values:
            self.wr(reg, apndp, v)

    def dpidr(self):
        return self.rd(0, DP)

    def sticky_error(self):
        return bool(self.rd(1, DP) & (1 << 5))

    def power_up(self):
        self.wr(1, DP, 0x50000000)  # CSYSPWRUPREQ | CDBGPWRUPREQ
        for _ in range(100):
            if (self.rd(1, DP) & 0xA0000000) == 0xA0000000:
                self.clear_errors()
                return
        raise DebugError("DAP power-up failed")

    def select(self, ap):
        if self.cur_ap != ap:
            self.cur_ap = None
            self.wr(2, DP, ap << 24)
            # 32-bit transfers; auto-increment on the memory AP only
            self.wr(0, AP, 0x00000002 if ap == AP_DM else 0x23000012)
            self.cur_ap = ap

    def ap_read32(self, ap, addr):
        self.select(ap)
        self.wr(1, AP, addr)
        return self.rd(3, AP)

    def ap_write32(self, ap, addr, val):
        self.select(ap)
        self.wr(1, AP, addr)
        self.wr(3, AP, val)

    def ap_read_repeat(self, ap, addr, n):
        """Read the same (non-incrementing) register n times, e.g. DM data0 with autoexec."""
        self.select(ap)
        self.wr(1, AP, addr)
        return self.rd_repeat(3, AP, n)

    def bus_read_words(self, addr, nwords):
        """Read words over the AHB-AP. The TAR auto-increment wraps at 1 KB."""
        self.select(AP_MEM)
        out = []
        while nwords:
            n = min(nwords, (0x400 - (addr & 0x3FF)) // 4)
            self.wr(1, AP, addr)
            out += self.rd_repeat(3, AP, n)
            addr += n * 4
            nwords -= n
        if self.sticky_error():
            self.clear_errors()
            raise DebugError("bus error reading 0x%08x" % addr)
        return out

    def bus_write_words(self, addr, words):
        self.select(AP_MEM)
        i = 0
        while i < len(words):
            n = min(len(words) - i, (0x400 - (addr & 0x3FF)) // 4)
            self.wr(1, AP, addr)
            self.wr_repeat(3, AP, words[i:i + n])
            addr += n * 4
            i += n
        if self.sticky_error():
            self.clear_errors()
            raise DebugError("bus error writing 0x%08x" % addr)


class _Batch:
    """Queued Debug Module register accesses, sent to the probe in as few transactions as possible."""

    def __init__(self):
        self.ops = []

    def w(self, off, val):
        self.ops += [(1, AP, off), (3, AP, val & 0xFFFFFFFF)]  # TAR, DRW
        return self

    def r(self, off):
        self.ops += [(1, AP, off), (3, AP, None)]
        return self

    def cmd(self, command):
        return self.w(COMMAND, command)


# Abstract commands (aarsize=32, transfer)
def _cmd_read(reg):
    return 0x00221000 | reg


def _cmd_write(reg):
    return 0x00231000 | reg


CMD_EXEC = 0x00261000         # harmless read of x0, then run the program buffer (this DM needs a transfer)
S1 = 9


def _csrr(rd, csr):
    return (csr << 20) | (2 << 12) | (rd << 7) | 0x73


def _csrw(csr, rs):
    return (csr << 20) | (rs << 15) | (1 << 12) | 0x73


def _csrsi(csr, imm):
    return (csr << 20) | (imm << 15) | (6 << 12) | 0x73


def _csrci(csr, imm):
    return (csr << 20) | (imm << 15) | (7 << 12) | 0x73


class Hart:
    """RISC-V debug operations for the single WS63 hart.

    While halted, all GPRs are read once into a cache. s0/s1 are then used freely as
    scratch registers for CSR and memory access and written back before the hart runs.
    """

    def __init__(self, dap):
        self.dap = dap
        self.dap.power_up()
        self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)
        self.dm_w(ABSTRACTAUTO, 0)
        self.dm_w(ABSTRACTCS, 0x700)
        self.num_triggers = None
        self.cache = None        # GPR values while halted
        self.clobbered = set()   # GPRs whose hardware value differs from the cache
        self.progbuf = [None] * 3

    # ---- Debug Module access ----
    def dm_r(self, off):
        return self.dap.ap_read32(AP_DM, off)

    def dm_w(self, off, val):
        self.dap.ap_write32(AP_DM, off, val)

    def _run(self, b, check=True):
        """Execute a batch. With check, verify afterwards that no abstract command failed."""
        if check:
            b.r(ABSTRACTCS)
        self.dap.select(AP_DM)
        try:
            vals = self.dap.transfer(b.ops)
        except DebugError:
            self.progbuf = [None] * 3
            raise
        if not check:
            return vals
        cs = vals.pop()
        for _ in range(100):
            if not cs & (1 << 12):  # busy
                break
            cs = self.dm_r(ABSTRACTCS)
        if cs & (1 << 12):
            self.progbuf = [None] * 3
            raise DebugError("abstract command busy timeout")
        err = (cs >> 8) & 7
        if err:
            self.dm_w(ABSTRACTCS, 0x700)
            self.progbuf = [None] * 3
            raise DebugError("abstract command failed, cmderr=%d" % err)
        return vals

    def _load_progbuf(self, b, insns):
        """Queue program buffer writes; entries that already hold the instruction are skipped."""
        for i, insn in enumerate(list(insns) + [EBREAK]):
            if self.progbuf[i] != insn:
                b.w(PROGBUF0 + 4 * i, insn)
                self.progbuf[i] = insn

    def status(self):
        return self.dm_r(DMSTATUS)

    def _forget_state(self):
        self.cache = None
        self.clobbered = set()
        self.progbuf = [None] * 3

    def reattach(self, timeout=10.0):
        """Wait for the debug port to come back (after a reset the firmware must re-enable
        the SWD pins) and re-initialise the DAP and the Debug Module."""
        deadline = time.time() + timeout
        while True:
            try:
                self.dap.reconnect()
                self.dap.power_up()
                self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)
                self.dm_w(ABSTRACTAUTO, 0)
                self.dm_w(ABSTRACTCS, 0x700)
                self._forget_state()
                return
            except DebugError:
                if time.time() > deadline:
                    raise DebugError("target did not come back within %.0f s" % timeout)
                time.sleep(0.2)

    def had_reset(self):
        """True (once) if the hart was reset since the last call; acknowledges it."""
        if not self.status() & DMSTATUS_ANYHAVERESET:
            return False
        self.dm_w(DMCONTROL, DMCONTROL_ACKHAVERESET | DMCONTROL_DMACTIVE)
        self._forget_state()
        return True

    def reset_halt(self):
        """System reset with haltreq held: the hart stops at the reset vector (0x100000),
        before the BootROM runs. The pin mux survives ndmreset, so the link stays up."""
        self.dm_w(DMCONTROL, DMCONTROL_HALTREQ | DMCONTROL_NDMRESET | DMCONTROL_DMACTIVE)
        time.sleep(0.01)
        self.dm_w(DMCONTROL, DMCONTROL_HALTREQ | DMCONTROL_DMACTIVE)
        for _ in range(100):
            if self.halted():
                break
        self.dm_w(DMCONTROL, DMCONTROL_ACKHAVERESET | DMCONTROL_DMACTIVE)
        self._forget_state()
        if not self.halted():
            raise DebugError("reset halt failed, dmstatus=%08x" % self.status())

    def reset(self, timeout=10.0):
        """Pulse ndmreset and reattach to the debug port. This does not guarantee that
        the application has booted: a later full-chip reset may still drop SWD."""
        try:
            self.dm_w(DMCONTROL, DMCONTROL_NDMRESET | DMCONTROL_DMACTIVE)
            self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)
        except DebugError:
            pass  # the link may drop as soon as the reset takes effect
        self._forget_state()
        time.sleep(0.3)
        self.reattach(timeout)
        self.had_reset()

    def halted(self):
        return bool(self.status() & DMSTATUS_ALLHALTED)

    def halt(self):
        self.dm_w(DMCONTROL, DMCONTROL_HALTREQ | DMCONTROL_ACKHAVERESET | DMCONTROL_DMACTIVE)
        for _ in range(100):
            if self.halted():
                break
        self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)
        if not self.halted():
            raise DebugError("halt failed, dmstatus=%08x" % self.status())

    def _queue_restore(self, b):
        """Queue writing the cached values back into the clobbered scratch registers."""
        for n in sorted(self.clobbered):
            b.w(DATA0, self.cache[n]).cmd(_cmd_write(n))

    def restore_registers(self):
        """Restore scratch GPRs without resuming an already halted target before closing."""
        if self.clobbered:
            b = _Batch()
            self._queue_restore(b)
            self._run(b)
            self.clobbered.clear()

    def resume(self, step=False):
        b = _Batch()
        if step:
            self._load_progbuf(b, [_csrsi(CSR_DCSR, DCSR_STEP)])
            b.cmd(CMD_EXEC)
        if self.cache is not None:
            self._queue_restore(b)
        self._run(b)
        self.clobbered = set()
        self.cache = None
        self.dm_w(DMCONTROL, DMCONTROL_RESUMEREQ | DMCONTROL_DMACTIVE)
        for _ in range(100):
            if self.status() & DMSTATUS_ALLRESUMEACK:
                break
        else:
            self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)
            raise DebugError("resume acknowledgement timeout")
        self.dm_w(DMCONTROL, DMCONTROL_DMACTIVE)

    def step(self):
        self.resume(step=True)
        for _ in range(100):
            if self.halted():
                break
        else:
            raise DebugError("step did not halt")
        b = _Batch()
        self._load_progbuf(b, [_csrci(CSR_DCSR, DCSR_STEP)])
        self._run(b.cmd(CMD_EXEC))

    # ---- registers ----
    def _regs(self):
        if self.cache is None:
            b = _Batch()
            for n in range(1, 32):
                b.cmd(_cmd_read(n)).r(DATA0)
            self.cache = [0] + self._run(b)
            self.clobbered = set()
        return self.cache

    def read_gpr(self, n):
        return self._regs()[n]

    def write_gpr(self, n, val):
        if n == 0:
            return
        regs = self._regs()
        self._run(_Batch().w(DATA0, val).cmd(_cmd_write(n)))
        regs[n] = val & 0xFFFFFFFF
        self.clobbered.discard(n)

    def _scratch(self, *regs):
        self._regs()  # make sure the real values are saved first
        self.clobbered.update(regs)

    def read_csrs(self, csrs):
        """Read several CSRs in one batch (through s0 and the program buffer)."""
        csrs = list(csrs)
        if any(not 0 <= csr <= 0xFFF for csr in csrs):
            raise DebugError("CSR number must be in 0x000..0xfff")
        self._scratch(S0)
        b = _Batch()
        for csr in csrs:
            self._load_progbuf(b, [_csrr(S0, csr)])
            b.cmd(CMD_EXEC).cmd(_cmd_read(S0)).r(DATA0)
        return self._run(b)

    def write_csrs(self, pairs):
        pairs = list(pairs)
        if any(not 0 <= csr <= 0xFFF for csr, _ in pairs):
            raise DebugError("CSR number must be in 0x000..0xfff")
        self._scratch(S0)
        b = _Batch()
        for csr, val in pairs:
            b.w(DATA0, val).cmd(_cmd_write(S0))
            self._load_progbuf(b, [_csrw(csr, S0)])
            b.cmd(CMD_EXEC)
        self._run(b)

    def read_csr(self, csr):
        return self.read_csrs([csr])[0]

    def write_csr(self, csr, val):
        self.write_csrs([(csr, val)])

    def read_fpr(self, n):
        self._scratch(S0)
        b = _Batch()
        self._load_progbuf(b, [0xE0000053 | (n << 15) | (S0 << 7)])  # fmv.x.w s0, fN
        return self._run(b.cmd(CMD_EXEC).cmd(_cmd_read(S0)).r(DATA0))[0]

    def write_fpr(self, n, val):
        self._scratch(S0)
        b = _Batch().w(DATA0, val).cmd(_cmd_write(S0))
        self._load_progbuf(b, [0xF0000053 | (S0 << 15) | (n << 7)])  # fmv.w.x fN, s0
        self._run(b.cmd(CMD_EXEC))

    def pc(self):
        return self.read_csr(CSR_DPC)

    def set_pc(self, val):
        self.write_csr(CSR_DPC, val)

    def halt_cause(self):
        """dcsr.cause: 1 ebreak, 2 trigger, 3 haltreq, 4 step, 5 resethaltreq."""
        return (self.read_csr(CSR_DCSR) >> 6) & 7

    # ---- memory ----
    def _pb_read_words(self, addr, n):
        """CPU-side read: lw s1,0(s0); addi s0,s0,4 re-run on every data0 read (autoexecdata)."""
        if not n:
            return []
        self._scratch(S0, S1)
        b = _Batch().w(DATA0, addr).cmd(_cmd_write(S0))
        self._load_progbuf(b, [0x00042483, 0x00440413])  # lw s1,0(s0); addi s0,s0,4
        b.cmd(CMD_EXEC)                                  # s1 = [addr], s0 = addr + 4
        if n == 1:
            return self._run(b.cmd(_cmd_read(S1)).r(DATA0))
        b.cmd(0x00261000 | S1)                           # data0 = word 0, s1 = word 1
        out = []
        try:
            if n > 2:
                b.w(ABSTRACTAUTO, 1)
            self._run(b)
            if n > 2:
                # Stop the pipeline before it loads beyond the requested range.
                out = self.dap.ap_read_repeat(AP_DM, DATA0, n - 2)
        finally:
            self.dm_w(ABSTRACTAUTO, 0)
        return out + self._run(_Batch().r(DATA0).cmd(_cmd_read(S1)).r(DATA0))

    def _pb_write_words(self, addr, words):
        """CPU-side write: sw s1,0(s0); addi s0,s0,4 re-run on every data0 write."""
        if not words:
            return
        self._scratch(S0, S1)
        b = _Batch().w(DATA0, addr).cmd(_cmd_write(S0))
        self._load_progbuf(b, [0x00942023, 0x00440413])  # sw s1,0(s0); addi s0,s0,4
        b.w(DATA0, words[0]).cmd(0x00271000 | S1)        # s1 = data0, then store it
        if len(words) > 1:
            b.w(ABSTRACTAUTO, 1)                         # every data0 write re-runs the command
            for w in words[1:]:
                b.w(DATA0, w)
            b.w(ABSTRACTAUTO, 0)
        try:
            self._run(b)
        finally:
            self.dm_w(ABSTRACTAUTO, 0)

    @staticmethod
    def _read_only(addr, n):
        """ROM and flash XIP can't hold dirty cache lines, so the bus view is coherent."""
        end = addr + 4 * n
        return (ROM_BASE <= addr and end <= ROM_END) or (FLASH_BASE <= addr and end <= FLASH_END)

    def read_words(self, addr, n):
        self._validate_range(addr, n * 4)
        if addr & 3:
            raise DebugError("word address must be aligned to 4 bytes")
        if not n:
            return []
        # The data cache is write-back: while halted, read RAM through the CPU so dirty
        # lines are seen. The AHB-AP bypasses the cache and would return stale data.
        if not self._read_only(addr, n) and self.halted():
            return self._pb_read_words(addr, n)
        return self.dap.bus_read_words(addr, n)

    def write_words(self, addr, words):
        self._validate_range(addr, len(words) * 4)
        if addr & 3:
            raise DebugError("word address must be aligned to 4 bytes")
        if not words:
            return
        if self.halted():
            self._pb_write_words(addr, words)
        else:
            self.dap.bus_write_words(addr, words)

    def read_mem(self, addr, length):
        self._validate_range(addr, length)
        if not length:
            return b""
        base = addr & ~3
        nwords = (addr + length - base + 3) // 4
        data = struct.pack("<%dI" % nwords, *self.read_words(base, nwords))
        return data[addr - base:addr - base + length]

    def write_mem(self, addr, data):
        self._validate_range(addr, len(data))
        if not data:
            return
        base = addr & ~3
        nwords = (addr + len(data) - base + 3) // 4
        if base == addr and len(data) == nwords * 4:
            buf = bytes(data)
        else:
            buf = bytearray(self.read_mem(base, nwords * 4))
            buf[addr - base:addr - base + len(data)] = data
        self.write_words(base, list(struct.unpack("<%dI" % nwords, bytes(buf))))

    @staticmethod
    def _validate_range(addr, length):
        if not 0 <= addr <= 0xFFFFFFFF or length < 0 or addr + length > 0x100000000:
            raise DebugError("memory range must fit the 32-bit address space")

    # ---- hardware triggers ----
    def count_triggers(self):
        if self.num_triggers is None:
            n = 0
            while n < 16:
                self.write_csr(CSR_TSELECT, n)
                sel, tdata1 = self.read_csrs([CSR_TSELECT, CSR_TDATA1])
                if sel != n or (tdata1 >> 28) != 2:
                    break
                n += 1
            self.num_triggers = n
        return self.num_triggers

    def set_trigger(self, idx, addr, kind, length=1):
        """Program an exact address or an aligned NAPOT data-address range."""
        if length <= 0 or length & (length-1) or addr < 0 or addr % length or addr+length > 0x100000000:
            raise ValueError('trigger range must be aligned and a power of two')
        if kind == 'exec' and length != 1:
            raise ValueError('execution triggers require an exact address')
        bits = {"exec": MCONTROL_EXECUTE, "store": MCONTROL_STORE, "load": MCONTROL_LOAD,
                "access": MCONTROL_LOAD | MCONTROL_STORE}[kind]
        if length > 1:
            bits |= 1 << 7  # mcontrol.match=NAPOT
            addr |= length//2-1
        self.write_csrs([(CSR_TSELECT, idx), (CSR_TDATA1, 0), (CSR_TDATA2, addr),
                         (CSR_TDATA1, MCONTROL_BASE | bits)])
        actual, address = self.read_csrs([CSR_TDATA1, CSR_TDATA2])
        if actual & 0xF800FFFF != (MCONTROL_BASE | bits) or address != addr:
            self.clear_trigger(idx)
            raise DebugError('hardware rejected trigger configuration')

    def clear_trigger(self, idx):
        self.write_csrs([(CSR_TSELECT, idx), (CSR_TDATA1, 0)])

    def trigger_hit(self, idx):
        self.write_csr(CSR_TSELECT, idx)
        return bool(self.read_csr(CSR_TDATA1) & MCONTROL_HIT)

    def sync_code(self):
        """WS63 cache maintenance: clean/invalidate D-cache, invalidate I-cache, fence.

        Uses only the debug program buffer, no functions in the target firmware.
        The CPU must be halted. Register values are preserved by the scratch cache.
        """
        if not self.halted():
            raise DebugError("cache maintenance requires a halted target")
        self.write_csr(0x7C3, 12)
        self.write_csr(0x7C2, 4)
        b = _Batch()
        self._load_progbuf(b, [0x0FF0000F, 0x0000100F])  # fence; fence.i
        self._run(b.cmd(CMD_EXEC))


def connect(speed=4000, serial=None):
    """Open the CMSIS-DAP probe (the one with this serial number, if given) and attach to the hart."""
    import cmsisdap
    dap = cmsisdap.CMSISDAP(speed, serial)
    try:
        return dap, Hart(dap)
    except Exception:
        dap.close()
        raise


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Check the WS63 debug connection")
    ap.add_argument("--serial", help="CMSIS-DAP serial number, if several are connected")
    ap.add_argument("--speed", type=int, default=4000, help="SWD clock in kHz")
    args = ap.parse_args()
    import ws63dbg  # use the importable module so exception classes match cmsisdap's
    dap, hart = ws63dbg.connect(args.speed, args.serial)
    print("probe    %s" % dap.name)
    print("DPIDR    %08x" % dap.dpidr())
    print("dmstatus %08x" % hart.status())
    dap.close()
