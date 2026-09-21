#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""GDB remote serial protocol server for WS63 over SWD, using a CMSIS-DAP probe.

Usage:
    python3 gdbserver.py [--port 3333] [--speed 4000] [--serial <probe serial>]
    riscv32-linux-musl-gdb -x ws63.gdbinit output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf

Supports hardware/software breakpoints, SFC Flash programming and optional
read-only LiteOS thread inspection with a matching ELF.
"""
import argparse
import json
from pathlib import Path
import shlex
import time
import select
import socket
import struct
import sys

from ws63dbg import (DebugError, connect, CSR_DCSR, DMSTATUS_ALLHALTED, DMSTATUS_ANYHAVERESET,
                     ROM_BASE, ROM_END, FLASH_BASE, FLASH_END)
from ws63break import SoftwareBreakpoints
from ws63flash import Flash, SECTOR, flash_range
from ws63watch import Watchpoints

NUM_GPRS = 32
REG_PC = 32
REG_FPR0 = 33
REG_CSR0 = 65  # gdb numbers CSR n as 65 + n
PACKET_SIZE = 0x4000
# Machine-mode CSRs exposed to gdb (read through the program buffer on demand)
CSRS = {"mstatus": 0x300, "misa": 0x301, "mie": 0x304, "mtvec": 0x305, "mscratch": 0x340, "mepc": 0x341,
        "mcause": 0x342, "mtval": 0x343, "mip": 0x344, "dcsr": 0x7B0, "dpc": 0x7B1}

GPR_NAMES = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "fp", "s1"] + \
    ["a%d" % i for i in range(8)] + ["s%d" % i for i in range(2, 12)] + ["t%d" % i for i in range(3, 7)]
FPR_NAMES = ["ft%d" % i for i in range(8)] + ["fs0", "fs1"] + ["fa%d" % i for i in range(8)] + \
    ["fs%d" % i for i in range(2, 12)] + ["ft%d" % i for i in range(8, 12)]


def target_xml():
    cpu = "".join('<reg name="%s" bitsize="32" regnum="%d" type="%s"/>' %
                  (n, i, "code_ptr" if n == "ra" else "data_ptr" if n in ("sp", "gp", "tp", "fp") else "int")
                  for i, n in enumerate(GPR_NAMES))
    cpu += '<reg name="pc" bitsize="32" regnum="32" type="code_ptr"/>'
    fpu = "".join('<reg name="%s" bitsize="32" regnum="%d" type="ieee_single"/>' % (n, REG_FPR0 + i)
                  for i, n in enumerate(FPR_NAMES))
    fpu += "".join('<reg name="%s" bitsize="32" regnum="%d" type="int"/>' % (n, r)
                   for n, r in (("fflags", REG_CSR0 + 1), ("frm", REG_CSR0 + 2), ("fcsr", REG_CSR0 + 3)))
    csr = "".join('<reg name="%s" bitsize="32" regnum="%d" type="int" group="system"/>' % (n, REG_CSR0 + c)
                  for n, c in CSRS.items())
    return ('<?xml version="1.0"?><!DOCTYPE target SYSTEM "gdb-target.dtd"><target version="1.0">'
            '<architecture>riscv:rv32</architecture>'
            '<feature name="org.gnu.gdb.riscv.cpu">%s</feature>'
            '<feature name="org.gnu.gdb.riscv.fpu">%s</feature>'
            '<feature name="org.gnu.gdb.riscv.csr">%s</feature></target>' % (cpu, fpu, csr))


def memory_map_xml(flash=False):
    # ROM and flash (XIP) are read-only to gdb, so it uses hardware breakpoints there.
    # Everything else is declared RAM so peripherals stay accessible.
    regions = [("ram", 0, ROM_BASE), ("rom", ROM_BASE, ROM_END - ROM_BASE),
               ("ram", ROM_END, FLASH_BASE - ROM_END), ("flash" if flash else "rom", FLASH_BASE, FLASH_END - FLASH_BASE),
               ("ram", FLASH_END, 0x100000000 - FLASH_END)]
    return ('<?xml version="1.0"?><!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
            '"http://sourceware.org/gdb/gdb-memory-map.dtd"><memory-map>' +
            "".join('<memory type="%s" start="0x%x" length="0x%x">' % r + ('<property name="blocksize">0x1000</property>' if r[0]=="flash" else "") + "</memory>" for r in regions) + "</memory-map>")


def hex32(v):
    return struct.pack("<I", v & 0xFFFFFFFF).hex()


def unescape(text):
    wire=text.encode('latin-1');out=bytearray();i=0
    while i<len(wire):
        value=wire[i];i+=1
        if value==0x7d:
            if i==len(wire):raise ValueError('truncated binary escape')
            value=wire[i]^0x20;i+=1
        out.append(value)
    return bytes(out)


class Server:
    def __init__(self, hart, verbose=False, flash=None, rtos=None, image=None, readonly=False, software_flash=False):
        self.h = hart
        self.verbose = verbose
        self.xml = target_xml().encode()
        self.flash=flash;self.rtos=rtos;self.image=image;self.readonly=readonly
        self.software=SoftwareBreakpoints(hart,flash if software_flash else None)
        self.flash_erases=[];self.flash_writes=[];self.needs_reset=False
        self.no_ack=False;self.start_no_ack=False;self.exception_snapshot=None
        self.exception_trigger=None
        self.memmap = memory_map_xml(flash is not None and not readonly).encode()
        self.breakpoints = {}  # addr -> trigger index
        self.watchpoints = Watchpoints(hart)
        self.watch_stop_pc = None
        self.watch_last = 'no data-trigger stop decoded'
        self.watch_filtered = 0
        self.sock = None
        self.interrupted = False

    # ---- packet I/O ----
    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        pkt = b"$" + data + b"#" + ("%02x" % (sum(data) & 0xFF)).encode()
        if self.verbose:
            print("<- %s" % pkt[:200])
        self.sock.sendall(pkt)
        if self.no_ack:return
        # The QStartNoAckMode response is the final acknowledged packet.
        while True:
            c = self.sock.recv(1)
            if not c or c == b"+":
                if self.start_no_ack:self.no_ack=True;self.start_no_ack=False
                return
            if c == b"-":
                self.sock.sendall(pkt)

    def recv_packet(self):
        while True:
            c = self.sock.recv(1)
            if not c:
                return None
            if c == b"\x03":
                return "\x03"
            if c != b"$":
                continue
            data = bytearray()
            checksum = 0
            oversized = False
            while True:
                c = self.sock.recv(1)
                if not c:
                    return None
                if c == b"#":
                    break
                checksum = (checksum + c[0]) & 0xFF
                if len(data) < PACKET_SIZE:
                    data += c
                else:
                    oversized = True
            trailer = b""
            while len(trailer) < 2:
                c = self.sock.recv(2 - len(trailer))
                if not c:
                    return None
                trailer += c
            try:
                valid = not oversized and int(trailer, 16) == checksum
            except ValueError:
                valid = False
            if not valid:
                if not self.no_ack:self.sock.sendall(b"-")
                continue
            if not self.no_ack:self.sock.sendall(b"+")
            if self.verbose:
                print("-> %s" % data[:200])
            return data.decode("latin-1")

    # ---- helpers ----
    def free_trigger(self):
        used = set(self.all_triggers())
        for i in range(self.h.count_triggers()):
            if i not in used:
                return i
        return None

    def all_triggers(self):
        return list(self.breakpoints.values()) + self.watchpoints.slots() + ([self.exception_trigger] if self.exception_trigger is not None else [])

    def disable_triggers(self):
        for idx in list(self.breakpoints.values()) + ([self.exception_trigger] if self.exception_trigger is not None else []):
            self.h.clear_trigger(idx)
        self.watchpoints.disable()

    def enable_triggers(self):
        for addr, idx in self.breakpoints.items():
            self.h.set_trigger(idx, addr, "exec")
        self.watchpoints.enable()
        if self.exception_trigger is not None:
            self.h.set_trigger(self.exception_trigger,self.exception_snapshot[0],'exec')

    def stop_reply(self):
        reply=self._stop_reason()
        if self.rtos:
            self.rtos.invalidate();self.rtos.refresh();self.rtos.selected=self.rtos.current
            if reply.startswith('S'):reply='T'+reply[1:3]
            reply+='thread:%x;'%self.rtos.current
        if self.exception_snapshot and self.exception_snapshot[1] and self.h.pc()==self.exception_snapshot[0]:
            from ws63diagnose import capture
            directory=Path(self.exception_snapshot[1])/time.strftime('%Y%m%d-%H%M%S')
            try:
                capture(self.h,self.image,directory,self.rtos)
                self.console('exception snapshot: '+str(directory)+'\n')
            except (DebugError,OSError) as error:self.console('snapshot failed: '+str(error)+'\n')
        return reply

    def _stop_reason(self):
        if self.readonly:return 'S05'
        if self.h.halt_cause()==1 and self.h.pc() in self.software.entries:return 'T05swbreak:;'
        cause = self.h.halt_cause()
        if cause == 2:  # trigger
            pc = self.h.pc()
            if self.execution_breakpoint_at(pc):
                return 'T05hwbreak:;'
            if self.watchpoints.entries:
                self.watch_stop_pc = pc
                access, matches, _ = self.watch_event(pc)
                if matches:
                    entry = matches[0]
                    tag = {"store": "watch", "load": "rwatch", "access": "awatch"}[entry.kind]
                    return 'T05%s:%x;' % (tag, max(access.address, entry.address))
            return 'S05'  # unknown instructions/causes must not be guessed
        if cause == 3:
            return "S02" if self.interrupted else "S05"
        return "S05"

    def execution_breakpoint_at(self, pc):
        return pc in self.breakpoints or self.exception_snapshot and pc == self.exception_snapshot[0]

    def watch_event(self, pc):
        try:
            access, matches, guarded = self.watchpoints.classify(pc, self.software.read)
        except DebugError as error:
            self.watch_last = 'pc=0x%x: cannot read instruction: %s' % (pc, error)
            return None, [], False
        if access is None:
            self.watch_last = 'pc=0x%x: unknown/non-scalar or unaligned access; target kept halted' % pc
        else:
            self.watch_last = 'pc=0x%x: %s 0x%x +%d, %d matching watchpoint(s)' % (
                pc, access.kind, access.address, access.size, len(matches))
        return access, matches, guarded

    def skip_watch_guard(self):
        """Step only a proven scalar access outside every requested range."""
        if not self.watchpoints.entries or self.h.halt_cause() != 2:
            return False
        pc = self.h.pc()
        if self.execution_breakpoint_at(pc):
            return False
        access, matches, guarded = self.watch_event(pc)
        if access is None or matches or not guarded:
            return False
        self.step_over()
        if self.h.halt_cause() != 4:
            return False
        self.watch_filtered += 1
        self.h.resume()
        return True

    def step_over(self):
        """Single-step with triggers disabled so a breakpoint at pc does not re-fire."""
        if self.watchpoints.unsafe:
            raise DebugError('watchpoint cleanup required before stepping')
        self.disable_triggers()
        try:
            with self.software.displaced(self.h.pc()):self.h.step()
            if self.rtos:self.rtos.invalidate()
        finally:
            self.enable_triggers()

    def do_continue(self):
        if self.readonly:raise DebugError('offline snapshot is read-only')
        if self.watchpoints.unsafe:
            raise DebugError('watchpoint cleanup failed; remove watchpoints before continuing')
        if self.needs_reset or self.flash and self.flash.unsafe:
            self.console('Flash changed or operation failed: verify/recover, then monitor reset halt before continue\n')
            return 'E01'
        self.interrupted = False
        pc = self.h.pc()
        if pc in self.breakpoints or pc in self.software.entries or self.exception_snapshot and pc==self.exception_snapshot[0] or pc == self.watch_stop_pc:
            self.step_over()
            if self.h.halt_cause() != 4:  # stopped for another reason while stepping
                return self.stop_reply()
        self.watch_stop_pc = None
        if self.rtos:self.rtos.invalidate()
        self.h.resume()
        while True:
            r, _, _ = select.select([self.sock], [], [], 0.05)
            if r:
                c = self.sock.recv(1)
                if not c:
                    self.h.halt()
                    return None
                if c == b"\x03":
                    self.interrupted = True
                    self.h.halt()
                    return self.stop_reply()
            try:
                st = self.h.status()
            except DebugError:
                # Link lost: a reset, or boot code (BootROM/flashboot) put the SWD pins back to
                # GPIO. Triggers keep working meanwhile, so the hart may already be halted at a
                # breakpoint. Wait for the firmware to re-enable the pins, then look again.
                if not self.wait_for_link():
                    return None
                st = self.h.status()
            if st & DMSTATUS_ANYHAVERESET:
                self.after_reset(resume=True)
                continue
            if st & DMSTATUS_ALLHALTED:
                if self.skip_watch_guard():
                    continue
                return self.stop_reply()

    def wait_for_link(self):
        """Wait (indefinitely) until the debug port answers again. Returns False if gdb went away."""
        self.console("ws63dbg: debug link lost, waiting for the firmware to re-enable SWD...\n")
        waited = 0.0
        while True:
            try:
                self.h.reattach(timeout=0)
                return True
            except DebugError:
                pass
            r, _, _ = select.select([self.sock], [], [], 0.5)
            waited += 0.5
            if r:
                c = self.sock.recv(1)
                if not c:
                    return False
                if c == b"\x03":
                    self.console("ws63dbg: cannot halt, the debug link is down. If the hart stopped at a "
                                 "breakpoint before the firmware enables SWD (GPIO_13/14), reset the board.\n")
            elif waited % 10 < 0.5:
                self.console("ws63dbg: still waiting for the debug link (%.0f s)\n" % waited)

    def console(self, text):
        """Print on the gdb console (allowed while gdb waits for a stop reply)."""
        self.send("O" + text.encode().hex())

    def after_reset(self, resume):
        """The hart was reset: hardware triggers are gone, so program them again."""
        if not self.h.halted():self.h.halt()
        self.watch_stop_pc = None
        dropped=self.software.reset()
        if dropped and self.sock:self.console('reset discarded %d RAM/software breakpoint(s); reinsert after code is loaded\n'%dropped)
        if self.rtos:self.rtos.invalidate(reset=True)
        self.needs_reset=False
        self.h.had_reset()
        if not self.h.halted():
            self.h.halt()
        self.enable_triggers()
        n = len(self.all_triggers())
        if resume:
            self.h.resume()
            self.console("ws63dbg: target reset detected, %d breakpoint(s)/watchpoint(s) restored\n" % n)

    def read_reg(self, n):
        if self.rtos and not self.rtos.live_selected():
            task=self.rtos.tasks.get(self.rtos.selected)
            value=task.registers.get(n) if task else None
            return hex32(value) if value is not None else 'xxxxxxxx'
        if n < NUM_GPRS:
            return hex32(self.h.read_gpr(n))
        if n == REG_PC:
            return hex32(self.h.pc())
        try:
            if REG_FPR0 <= n < REG_FPR0 + 32:
                return hex32(self.h.read_fpr(n - REG_FPR0))
            if n >= REG_CSR0:
                return hex32(self.h.read_csr(n - REG_CSR0))
        except DebugError:
            pass  # e.g. FPU disabled (mstatus.FS=0) or CSR not implemented
        return "x" * 8

    def write_reg(self, n, val):
        if self.readonly or self.rtos and not self.rtos.live_selected():
            raise DebugError('register writes require the live current task')
        if 0 <= n < NUM_GPRS:
            self.h.write_gpr(n, val)
        elif n == REG_PC:
            self.h.set_pc(val)
        elif REG_FPR0 <= n < REG_FPR0 + 32:
            self.h.write_fpr(n - REG_FPR0, val)
        elif REG_CSR0 <= n <= REG_CSR0 + 0xFFF:
            self.h.write_csr(n - REG_CSR0, val)
        else:
            raise DebugError("register %d not writable" % n)

    def monitor(self, cmd):
        args = shlex.split(cmd)
        if not args:
            return ("commands:\n"
                    "  reset [halt]        reset the chip and stop at the reset vector (0x100000)\n"
                    "  csr <num> [value]   read / write a CSR\n"
                    "  triggers            hardware trigger usage\n"
                    "  watchpoints         data ranges, hardware slots and last decoded access\n"
                    "  dcsr                show dcsr\n"
                    "  tasks [water]       LiteOS task list / stack high-water estimate\n"
                    "  bt [all|task ID]     CFI backtraces (requires --elf)\n"
                    "  sync                mutex/semaphore waits and owner cycles\n"
                    "  snapshot <dir>      capture registers, RAM, tasks and diagnostics\n"
                    "  exceptions [off|dir] stop at SDK exception handler; optional snapshot\n"
                    "  flash info|write <addr> <bin>|restore <journal>\n"
                    "  md <addr> [count]   explicit aligned 32-bit MMIO reads\n"
                    "  mw <addr> <value>   explicit aligned 32-bit MMIO write\n")
        if args[0] in ('tasks','bt','sync','snapshot','exceptions','flash','md','mw'):
            return self.diagnostic_command(args)
        if self.readonly:return 'offline snapshot: read-only commands only\n'
        if args[0] == "reset":
            if len(args) > 1 and args[1] != "halt":
                return "only 'reset' / 'reset halt' is supported\n"
            if self.flash and self.flash.unsafe:raise DebugError('recover failed Flash transaction before reset')
            self.software.clear()
            self.h.reset_halt()
            self.after_reset(resume=False)
            return ("reset done, halted at pc=0x%08x; run 'maintenance flush register-cache' "
                    "(or the 'reset' command from ws63.gdbinit)\n" % self.h.pc())
        if args[0] == "csr" and len(args) >= 2:
            csr = int(args[1], 0)
            if len(args) == 3:
                self.h.write_csr(csr, int(args[2], 0))
            return "csr 0x%03x = 0x%08x\n" % (csr, self.h.read_csr(csr))
        if args[0] == "triggers":
            return "hardware triggers: %d (in use %d)\n" % (self.h.count_triggers(), len(self.all_triggers()))
        if args[0] == 'watchpoints':
            rows = ['%s 0x%x +%d: %s' % (e.kind, e.address, e.length,
                    ', '.join('slot %d [0x%x,0x%x)' % (i,a,a+n) for i,a,n in e.blocks))
                    for e in self.watchpoints.entries.values()]
            return '\n'.join(rows + ['filtered guard stops: %d' % self.watch_filtered, self.watch_last])+'\n'
        if args[0] == "dcsr":
            return "dcsr = 0x%08x\n" % self.h.read_csr(CSR_DCSR)
        return "unknown monitor command: %s\n" % cmd

    def diagnostic_command(self,args):
        name=args[0]
        if name in ('tasks','bt','sync'):
            if not self.rtos:return 'start server with --elf for LiteOS inspection\n'
            self.rtos.refresh()
            if name=='tasks':return self.rtos.listing(len(args)>1 and args[1]=='water')
            if name=='sync':return json.dumps(self.rtos.synchronization(),ensure_ascii=False,indent=2)+'\n'
            target=args[1] if len(args)>1 else 'current'
            tasks=[t for t in self.rtos.tasks.values() if target=='all' or
                   target=='current' and t.current or target not in ('all','current') and t.ident==int(target,0)]
            if not tasks:return 'no such task\n'
            text=[]
            for task in tasks:
                text.append('task %d %s'%(task.ident,task.name))
                frames,reason=self.rtos.backtrace(task)
                text += ['  #%d 0x%08x sp=0x%08x %s'%(i,f['pc'],f['sp'],f['symbol']) for i,f in enumerate(frames)]
                text.append('  stop: '+reason)
            return '\n'.join(text)+'\n'
        if name=='snapshot' and len(args)==2:
            if self.readonly:return 'snapshot already offline\n'
            from ws63diagnose import capture
            return str(capture(self.h,self.image,args[1],self.rtos))+'\n'
        if self.readonly:return 'offline snapshot: read-only commands only\n'
        if name=='exceptions':
            if len(args)>1 and args[1]=='off':
                if self.exception_snapshot:
                    self.h.clear_trigger(self.exception_trigger)
                self.exception_trigger=None
                self.exception_snapshot=None
                return 'exception catch disabled\n'
            if not self.image:raise DebugError('exception catch requires --elf')
            addr=self.image.symbol('OsExcHandleEntry')
            if self.exception_trigger is None:
                idx=self.free_trigger()
                if idx is None:raise DebugError('no free trigger for exception entry')
                self.h.set_trigger(idx,addr,'exec');self.exception_trigger=idx
            self.exception_snapshot=(addr,args[1] if len(args)>1 else None)
            return 'catch OsExcHandleEntry at 0x%x; inspect mepc, mcause and mtval\n'%addr
        if name=='flash' and self.flash:
            if self.software.entries:raise DebugError('remove software breakpoints before Flash operations')
            action=args[1]
            if action=='info':return 'JEDEC=%06x SR1/SR2=%02x/%02x\n'%((self.flash.identify(),)+self.flash.status())
            if action=='write' and len(args)==4:
                result=self.flash.apply([(int(args[2],0),Path(args[3]).read_bytes())])
            elif action=='restore' and len(args)==3:result=self.flash.restore(args[2])
            else:return 'flash info | write <XIP address> <bin> | restore <journal dir>\n'
            self.needs_reset=True
            if self.rtos:self.rtos.invalidate(reset=True)
            return json.dumps(result)+'; reset required before continue\n'
        if name in ('md','mw'):
            address=int(args[1],0)
            if address%4 or not 0<=address<=0xfffffffc:raise DebugError('requires aligned 32-bit address')
            from ws63dbg import AP_MEM
            if name=='mw':
                value=int(args[2],0)
                if len(args)!=3 or not 0<=value<=0xffffffff:raise DebugError('invalid value')
                if address<FLASH_END and address+4>ROM_BASE:raise DebugError('use RAM or MMIO address')
                self.h.dap.ap_write32(AP_MEM,address,value)
                return 'wrote 0x%08x (no implicit readback)\n'%value
            count=int(args[2],0) if len(args)==3 else 1
            if not 1<=count<=256 or address+4*count>0x100000000:raise DebugError('invalid count')
            return ''.join('0x%08x: 0x%08x\n'%(address+4*i,self.h.dap.ap_read32(AP_MEM,address+4*i)) for i in range(count))
        return 'invalid diagnostic command\n'

    def flash_packet(self,pkt):
        if not self.flash or self.readonly:return ''
        if self.software.entries:return 'E01'
        if pkt.startswith('vFlashErase:'):
            address,length=(int(v,16) for v in pkt.split(':',1)[1].split(','))
            flash_range(address,length)
            if address%SECTOR or length%SECTOR:return 'E01'
            if any(address<a+n and address+length>a for a,n in self.flash_erases):return 'E01'
            self.flash_erases.append((address,length));return 'OK'
        if pkt.startswith('vFlashWrite:'):
            address,wire=pkt[len('vFlashWrite:'):].split(':',1)
            address=int(address,16);data=unescape(wire);flash_range(address,len(data))
            if self.flash_writes and address<self.flash_writes[-1][0]+len(self.flash_writes[-1][1]):return 'E01'
            end=address+len(data);covered=address
            for a,n in sorted(self.flash_erases):
                if a<=covered<a+n:covered=min(end,a+n)
            if covered!=end:return 'E01'
            self.flash_writes.append((address,data));return 'OK'
        if pkt=='vFlashDone':
            if not self.flash_erases:return 'OK'
            sectors={a:bytearray(b'\xff'*SECTOR) for start,n in self.flash_erases for a in range(start,start+n,SECTOR)}
            for address,data in self.flash_writes:
                for i in range(0,len(data)):
                    a=address+i;sectors[a&-SECTOR][a%SECTOR]=data[i]
            try:
                result=self.flash.apply([(a,bytes(b)) for a,b in sorted(sectors.items())])
                # GDB also changes PC after load, even if every sector already
                # matched. Never resume a firmware image at that synthetic PC.
                self.needs_reset=True
                if self.rtos:self.rtos.invalidate(reset=True)
                return 'OK'
            finally:
                self.flash_erases=[];self.flash_writes=[]
        return ''

    # ---- request dispatch ----
    def handle(self, pkt):
        try:
            return self._handle(pkt)
        except (ValueError, IndexError, KeyError, OSError, struct.error):
            return "E01"

    def _handle(self, pkt):
        if not pkt:
            return ""
        c = pkt[0]
        if c == "?":
            return self.stop_reply()
        if c == "g":
            return "".join(self.read_reg(i) for i in range(33))
        if c == "G":
            data = bytes.fromhex(pkt[1:])
            if len(data) != 33 * 4:
                return "E01"
            for i in range(33):
                self.write_reg(i, struct.unpack_from("<I", data, i * 4)[0])
            if self.rtos:self.rtos.invalidate()
            return "OK"
        if c == "p":
            return self.read_reg(int(pkt[1:], 16))
        if c == "P":
            n, v = pkt[1:].split("=")
            self.write_reg(int(n, 16), struct.unpack("<I", bytes.fromhex(v))[0])
            if self.rtos:self.rtos.invalidate()
            return "OK"
        if c == "m":
            addr, length = (int(x, 16) for x in pkt[1:].split(","))
            if not 0 <= length <= PACKET_SIZE // 2:
                return "E14"
            try:
                return self.software.read(addr, length).hex()
            except DebugError:
                return "E14"
        if c == "M":
            head, data = pkt[1:].split(":")
            addr, length = (int(x, 16) for x in head.split(","))
            data = bytes.fromhex(data)
            if len(data) != length:
                return "E01"
            try:
                if self.readonly or (addr<FLASH_END and addr+length>FLASH_BASE):return 'E14'
                self.software.write(addr, data)
                return "OK"
            except DebugError:
                return "E14"
        if pkt.startswith('X'):
            head,wire=pkt[1:].split(':',1)
            addr,length=(int(x,16) for x in head.split(','))
            data=unescape(wire)
            if len(data)!=length:return 'E01'
            if self.readonly or (addr<FLASH_END and addr+length>FLASH_BASE):return 'E14'
            self.software.write(addr,data)
            return 'OK'
        if pkt.startswith('vFlash'):
            return self.flash_packet(pkt)
        if c == "c":
            if self.readonly or self.needs_reset or self.watchpoints.unsafe or self.flash and self.flash.unsafe:return 'E01'
            if len(pkt) > 1:
                self.h.set_pc(int(pkt[1:], 16))
            return self.do_continue()
        if c == "s":
            if self.readonly or self.needs_reset or self.watchpoints.unsafe or self.flash and self.flash.unsafe:return 'E01'
            if self.rtos and not self.rtos.live_selected():return 'E01'
            if len(pkt) > 1:
                self.h.set_pc(int(pkt[1:], 16))
            self.interrupted = False
            self.step_over()
            return self.stop_reply()
        if c in "Zz":
            if self.readonly:return 'E01'
            typ, addr, length = (int(x, 16) for x in pkt[1:].split(","))
            if typ==0:
                if c=='z' and addr in self.software.entries:
                    self.software.remove(addr);return 'OK'
                if c=='Z' and (0x14c000<=addr<0x180000 or 0xa00000<=addr<0xa88000 or self.software.flash and FLASH_BASE<=addr<FLASH_END):
                    try:self.software.insert(addr,length);return 'OK'
                    except DebugError:
                        if addr in self.software.entries:raise
                        # PMP-protected RAM uses a hardware trigger instead.
            kind = {0: "exec", 1: "exec", 2: "store", 3: "load", 4: "access"}.get(typ)
            if kind is None:
                return ""
            if not 0 <= addr <= 0xFFFFFFFF or length <= 0 or addr+length > 0x100000000:
                return "E01"
            if kind != 'exec':
                if c == 'z':
                    self.watchpoints.remove(addr, kind, length)
                    return 'OK'
                return 'OK' if self.watchpoints.insert(addr, kind, length, self.all_triggers()) else 'E0E'
            table, key = self.breakpoints, addr
            if c == "Z":
                if key in table:
                    return "OK"
                idx = self.free_trigger()
                if idx is None:
                    return "E0E"  # out of hardware triggers
                self.h.set_trigger(idx, addr, kind)
                table[key] = idx
            elif key in table:
                self.h.clear_trigger(table.pop(key))
            return "OK"
        if c == "D":
            if self.readonly:return 'OK'
            self.software.clear()
            self.disable_triggers()
            self.breakpoints.clear()
            self.watchpoints.clear()
            self.exception_trigger=None;self.exception_snapshot=None
            if not self.needs_reset and not (self.flash and self.flash.unsafe):self.h.resume()
            return "OK"
        if c == "k":
            if self.readonly:return None
            self.software.clear()
            self.disable_triggers()
            self.watchpoints.clear()
            self.breakpoints.clear()
            if not self.needs_reset and not (self.flash and self.flash.unsafe):self.h.resume()
            return None
        if c == 'H':
            thread=int(pkt[2:],16)
            if self.rtos:
                if pkt[1]=='g':self.rtos.select(thread)
                elif pkt[1]=='c' and thread not in (0,-1,self.rtos.current):return 'E01'
                return 'OK'
            return 'OK' if thread in (0,-1,1) else 'E01'
        if c == 'T':
            thread=int(pkt[1:],16)
            return 'OK' if thread in (self.rtos.threads() if self.rtos else [1]) else 'E01'
        if pkt=='QStartNoAckMode':self.start_no_ack=True;return 'OK'
        if pkt.startswith("qSupported"):
            return "PacketSize=4000;qXfer:features:read+;qXfer:memory-map:read+;hwbreak+;swbreak+;QStartNoAckMode+"
        if pkt.startswith("qXfer:memory-map:read::"):
            off, length = (int(x, 16) for x in pkt.split(":")[4].split(","))
            chunk = self.memmap[off:off + length]
            return ("l" if off + length >= len(self.memmap) else "m") + chunk.decode()
        if pkt.startswith("qXfer:features:read:target.xml:"):
            off, length = (int(x, 16) for x in pkt.split(":")[4].split(","))
            chunk = self.xml[off:off + length]
            return ("l" if off + length >= len(self.xml) else "m") + chunk.decode()
        if pkt == "qAttached":
            return "1"
        if pkt == "qC":
            if self.rtos:self.rtos.refresh()
            return 'QC%x'%(self.rtos.current if self.rtos else 1)
        if pkt.startswith('qThreadExtraInfo,'):
            thread=int(pkt.split(',')[1],16)
            return (self.rtos.extra(thread) if self.rtos else 'WS63 hart').encode().hex()
        if pkt == "qfThreadInfo":
            return "m"+",".join("%x"%i for i in (self.rtos.threads() if self.rtos else [1]))
        if pkt == "qsThreadInfo":
            return "l"
        if pkt.startswith("qRcmd,"):
            out = self.monitor(bytes.fromhex(pkt[6:]).decode())
            self.send("O" + out.encode().hex())
            return "OK"
        return ""  # unsupported

    def serve(self, port):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        print("WS63 GDB server (%s) listening on 127.0.0.1:%d" % (self.h.dap.name, port), flush=True)
        while True:
            self.sock, peer = srv.accept()
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("gdb connected from %s:%d" % peer, flush=True)
            self.interrupted = False
            self.no_ack=False;self.start_no_ack=False
            self.flash_erases=[];self.flash_writes=[]
            try:
                self.h.had_reset()  # acknowledge resets that happened before gdb attached
                self.h.halt()
                print("target halted at pc=0x%08x, %d hardware triggers" % (self.h.pc(), self.h.count_triggers()), flush=True)
                while True:
                    pkt = self.recv_packet()
                    if pkt is None:
                        break
                    if pkt == "\x03":
                        self.interrupted = True
                        self.h.halt()
                        self.send(self.stop_reply())
                        continue
                    try:
                        reply = self.handle(pkt)
                    except DebugError as e:
                        print("error: %s" % e, flush=True)
                        reply = "E01"
                    if reply is None:
                        break
                    self.send(reply)
                    if pkt and pkt[0] in "Dk":
                        break
            except (ConnectionError, OSError, DebugError) as e:
                print("connection lost: %s" % e, flush=True)
            finally:
                # Restore patched instructions; failed/changed firmware stays halted.
                try:
                    if not self.readonly:
                        if not self.h.halted():self.h.halt()
                        self.software.clear()
                    self.disable_triggers()
                    self.breakpoints.clear()
                    self.watchpoints.clear()
                    self.exception_trigger=None;self.exception_snapshot=None
                    if not self.readonly and not self.needs_reset and not (self.flash and self.flash.unsafe):
                        self.h.resume()
                    else:self.h.restore_registers()
                except DebugError as e:
                    print("gdb disconnected; target cleanup failed: %s (reset the board)" % e, flush=True)
                else:
                    print("gdb disconnected, target "+("halted/read-only" if self.readonly or self.needs_reset or self.flash and self.flash.unsafe else "running"), flush=True)
                finally:
                    self.sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--serial", help="CMSIS-DAP serial number, if several are connected")
    ap.add_argument("--speed", type=int, default=4000, help="SWD clock in kHz")
    ap.add_argument("-v", "--verbose", action="store_true", help="log RSP packets")
    ap.add_argument('--elf',help='matching ELF (prepared debug ELF recommended for LiteOS/CFI)')
    ap.add_argument('--no-rtos',action='store_true',help='disable LiteOS even with --elf')
    ap.add_argument('--no-flash',action='store_true',help='advertise Flash as read-only')
    ap.add_argument('--flash-journal',default='artifacts/flash')
    ap.add_argument('--software-flash-breakpoints',action='store_true',help='allow Z0 instruction patching in Flash (sector writes)')
    args = ap.parse_args()
    try:
        dap, hart = connect(args.speed, args.serial)
    except DebugError as e:
        sys.exit("connect failed: %s" % e)
    try:
        image=None;rtos=None
        if args.elf:
            from ws63elf import Image
            image=Image(args.elf)
            if not args.no_rtos:
                from ws63rtos import LiteOS
                rtos=LiteOS(hart,image)
        flash=None if args.no_flash else Flash(hart,args.flash_journal,lambda s:print(s,flush=True))
        Server(hart,args.verbose,flash=flash,rtos=rtos,image=image,software_flash=args.software_flash_breakpoints).serve(args.port)
    except KeyboardInterrupt:
        pass
    finally:
        dap.close()


if __name__ == "__main__":
    main()
