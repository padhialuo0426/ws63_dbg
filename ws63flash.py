#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""WS63 GD25Q32 Flash programming through SFC registers over the AHB-AP.

Requires SWD and an already configured SFC/XIP controller, but no ELF, target
function addresses or target RAM loader. All addresses exposed to users are XIP.
"""
import argparse
import binascii
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import uuid

from ws63dbg import AP_MEM, DebugError, FLASH_BASE, FLASH_END, connect

SECTOR = 4096
SFC = 0x48000000


def controller_session(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.controller():
            return method(self, *args, **kwargs)
    return wrapped


def flash_range(address, length):
    if length <= 0 or not FLASH_BASE <= address < address + length <= FLASH_END:
        raise DebugError('Flash range must be inside 0x200000..0x5fffff')


def validate_chunks(chunks):
    chunks = sorted((int(a), bytes(b)) for a, b in chunks)
    end = FLASH_BASE
    for address, data in chunks:
        flash_range(address, len(data))
        if address < end:
            raise DebugError('overlapping Flash writes')
        end = address + len(data)
    if not chunks:
        raise DebugError('no Flash data')
    return chunks


def read_package(path):
    """Parse the SDK FWPKG container, retaining signed images as opaque bytes.

    Type zero is the UART RAM loader and is not programmed. Type one contains
    Flash images. Header CRC, extents, types and Flash overlaps are checked.
    """
    data = Path(path).read_bytes()
    if len(data) < 12:
        raise DebugError('truncated FWPKG')
    magic, crc, count, size = struct.unpack_from('<IHHI', data)
    head = 12 + count * 52
    if magic != 0xEFBEADDF or size != len(data) or not 1 <= count <= 64 or head > size:
        raise DebugError('invalid FWPKG header')
    if binascii.crc_hqx(data[6:head], 0) != crc:
        raise DebugError('FWPKG header CRC mismatch')
    chunks, names, extents = [], [], []
    for i in range(count):
        name, off, length, address, capacity, typ = struct.unpack_from('<32sIIIII', data, 12 + 52*i)
        if length <= 0 or off < head or off + length + 16 > size:
            raise DebugError('invalid FWPKG image extent')
        if any(off < end and off + length + 16 > start for start, end in extents):
            raise DebugError('overlapping FWPKG file extents')
        extents.append((off, off + length + 16))
        if data[off+length:off+length+16] != bytes(16):
            raise DebugError('invalid FWPKG image padding')
        if typ == 0:
            continue
        if typ != 1 or length > capacity:
            raise DebugError('unsupported FWPKG image type or size')
        flash_range(address, capacity)
        chunks.append((address, data[off:off+length]))
        names.append(name.split(b'\0')[0].decode('utf-8', 'replace'))
    ordered=sorted(zip(chunks,names),key=lambda pair:pair[0][0])
    return validate_chunks([c for c,n in ordered]),[n for c,n in ordered]


def durable_write(path, data):
    with open(path, 'xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


class Flash:
    def __init__(self, hart, journal_root='artifacts/flash', progress=None):
        self.h = hart
        self.d = hart.dap
        self.journal_root = Path(journal_root)
        self.progress = progress or (lambda message: None)
        self.last_journal = None
        self.unsafe = False
        self._controller_depth = 0

    def rd(self, off):
        return self.d.ap_read32(AP_MEM, SFC + off)

    def wr(self, off, value):
        self.d.ap_write32(AP_MEM, SFC + off, value)

    def idle(self, seconds=1):
        deadline = time.monotonic() + seconds
        while self.rd(0x300) & 1:
            if time.monotonic() >= deadline:
                raise DebugError('SFC command timeout')

    @contextmanager
    def controller(self):
        """Borrow SFC without corrupting a firmware operation paused mid-call.

        Preserve command registers, data buffer and WEL. The target may have
        prepared a command or enabled writes immediately before it was halted.
        Nested host operations share one saved controller state.
        """
        if self._controller_depth:
            self._controller_depth += 1
            try:yield
            finally:self._controller_depth -= 1
            return
        if not self.h.halted():raise DebugError('SFC access requires halt')
        if self.rd(0x240)&1:raise DebugError('SFC DMA is active')
        self.idle()
        saved={off:self.rd(off) for off in (0x300,0x308,0x30c)}
        data=self.d.bus_read_words(SFC+0x400,16)
        self._controller_depth=1
        wel=None
        try:
            self.ready()
            wel=self._command(0x05,read=1)[0]&2
            yield
        finally:
            try:
                self.ready()
                if wel is not None:
                    now=self._command(0x05,read=1)[0]&2
                    if now!=wel:self._command(0x06 if wel else 0x04)
                self.d.bus_write_words(SFC+0x400,data)
                self.wr(0x308,saved[0x308]);self.wr(0x30c,saved[0x30c])
                self.wr(0x300,saved[0x300]&~1)
            except BaseException:
                self.unsafe=True
                raise
            finally:self._controller_depth=0

    @controller_session
    def command(self, opcode, address=None, data=b'', read=0):
        return self._command(opcode,address,data,read)

    def _command(self, opcode, address=None, data=b'', read=0):
        if len(data) > 64 or not 0 <= read <= 64 or (data and read):
            raise DebugError('invalid SFC command transfer')
        self.idle()
        if address is not None:
            self.wr(0x30C, address)
        if data:
            padded = data + bytes((-len(data)) % 4)
            self.d.bus_write_words(SFC+0x400, struct.unpack('<%dI' % (len(padded)//4), padded))
        self.wr(0x308, opcode)
        length = read or len(data)
        # SDK CS=1, standard SPI, no dummy bytes, command start.
        config = 3 | (8 if address is not None else 0)
        if length:
            config |= 128 | ((length-1) << 9) | (256 if read else 0)
        self.wr(0x300, config)
        self.idle()
        if read:
            words = self.d.bus_read_words(SFC+0x400, (read+3)//4)
            return struct.pack('<%dI' % len(words), *words)[:read]
        return b''

    def status(self):
        return self.command(0x05, read=1)[0], self.command(0x35, read=1)[0]

    def ready(self, seconds=5):
        deadline = time.monotonic() + seconds
        while self.command(0x05, read=1)[0] & 1:
            if time.monotonic() >= deadline:
                raise DebugError('Flash busy timeout; leave target halted')
            time.sleep(.001)

    @controller_session
    def identify(self):
        if not self.h.halted():
            raise DebugError('Flash access requires a halted target')
        if self.rd(0x240) & 1:
            raise DebugError('SFC DMA is active')
        if self.rd(0x218) != FLASH_BASE or ((self.rd(0x210) >> 8) & 15) != 7:
            raise DebugError('unsupported SFC mapping: requires CS1, 4 MiB at 0x200000')
        self.ready()
        ident = int.from_bytes(self.command(0x9F, read=3), 'little')
        if ident != 0x1640C8:
            raise DebugError('unsupported JEDEC ID 0x%06x (requires GD25Q32)' % ident)
        return ident

    @controller_session
    def set_status(self, sr1, sr2):
        # Volatile status writes preserve QE and all non-protection bits.
        for cmd, val in ((0x01, sr1 & ~3), (0x31, sr2)):
            self.ready()
            self.command(0x50)
            self.command(cmd, data=bytes([val]))
            self.ready()
        a, b = self.status()
        if (a & ~3, b) != (sr1 & ~3, sr2):
            raise DebugError('Flash status/protection write did not take effect')

    @contextmanager
    def writable(self):
        self.identify()
        saved = self.status()
        try:
            self.set_status(saved[0] & ~0x7C, saved[1] & ~0x40)
            yield
        finally:
            self.ready()
            self.set_status(*saved)

    def read(self, address, length):
        flash_range(address, length)
        return self.h.read_mem(address, length)

    @controller_session
    def read_spi(self, address, length):
        flash_range(address, length)
        out = bytearray()
        for i in range(0, length, 64):
            out += self.command(0x03, address+i-FLASH_BASE, read=min(64, length-i))
        return bytes(out)

    def write_enable(self):
        self.ready()
        self.command(0x06)
        if not self.command(0x05, read=1)[0] & 2:
            raise DebugError('Flash WEL did not set')

    def write_sector(self, address, data):
        if address % SECTOR or len(data) != SECTOR:
            raise DebugError('sector alignment/size error')
        flash_range(address, SECTOR)
        self.write_enable()
        self.command(0x20, address-FLASH_BASE)
        self.ready()
        for offset in range(0, SECTOR, 64):
            block = data[offset:offset+64]
            if block == b'\xff'*64:
                continue
            self.write_enable()
            self.command(0x02, address+offset-FLASH_BASE, data=block)
            self.ready()
        if self.read(address, SECTOR) != data:
            raise DebugError('Flash verification failed at 0x%x' % address)

    @controller_session
    def apply(self, chunks, force=False):
        """Preserve uncovered sector bytes. Durable backups precede ALL erases.

        No automatic resume or reset: callers decide after the image is verified.
        On failure a recovery journal remains and unsafe prevents server resume.
        """
        chunks = validate_chunks(chunks)
        self.identify()
        before, after = {}, {}
        for address, data in chunks:
            for sector in range(address & -SECTOR, (address+len(data)+SECTOR-1) & -SECTOR, SECTOR):
                if sector not in before:
                    before[sector] = self.read(sector, SECTOR)
                    after[sector] = bytearray(before[sector])
                start, end = max(address, sector), min(address+len(data), sector+SECTOR)
                after[sector][start-sector:end-sector] = data[start-address:end-address]
        changed = [a for a in sorted(after) if force or after[a] != before[a]]
        if not changed:
            return dict(sectors=0, journal=None)
        root = self.journal_root / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
        root.mkdir(parents=True, exist_ok=False)
        manifest = dict(version=1, jedec='1640c8', state='prepared', status=list(self.status()), sectors=[])
        for a in changed:
            filename = '%08x.bin' % a
            durable_write(root/filename, before[a])
            manifest['sectors'].append(dict(address=a, file=filename,
                sha256=hashlib.sha256(before[a]).hexdigest(), new_sha256=hashlib.sha256(after[a]).hexdigest()))
        durable_write(root/'journal.json', json.dumps(manifest, indent=2).encode())
        # Persist the new directory entries too: syncing files and the leaf
        # alone does not make a newly created journal directory crash-durable.
        for directory in (root.resolve(), *root.resolve().parents):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        self.last_journal = str(root)
        self.progress('backup: '+str(root))
        self.unsafe = True
        try:
            self.h.sync_code()
            with self.writable():
                for i, a in enumerate(changed):
                    self.write_sector(a, bytes(after[a]))
                    if i % 16 == 0 or i+1 == len(changed):
                        self.progress('verified %d/%d sectors' % (i+1, len(changed)))
            self.h.sync_code()
            durable_write(root/'complete.json', json.dumps(dict(state='verified', sectors=len(changed))).encode())
            self.unsafe = False
            return dict(sectors=len(changed), journal=str(root))
        except BaseException as error:
            try:durable_write(root/'failure.txt', (type(error).__name__+': '+str(error)).encode())
            except OSError:pass  # Backups already exist; do not hide the first failure.
            raise

    @controller_session
    def restore(self, directory):
        root = Path(directory)
        manifest = json.loads((root/'journal.json').read_text())
        if manifest.get('version') != 1 or manifest.get('jedec') != '1640c8':
            raise DebugError('unsupported recovery journal')
        chunks = []
        for item in manifest['sectors']:
            filename = item['file']
            if Path(filename).name != filename:
                raise DebugError('invalid journal file path')
            data = (root/filename).read_bytes()
            if len(data) != SECTOR or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise DebugError('corrupt recovery backup')
            chunks.append((item['address'], data))
        result=self.apply(chunks)
        if 'status' in manifest:
            self.unsafe=True
            self.set_status(*manifest['status'])
            self.unsafe=False
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--serial')
    ap.add_argument('--speed', type=int, default=4000)
    ap.add_argument('--journal-dir', default='artifacts/flash')
    ap.add_argument('--resume', action='store_true', help='resume original PC after successful operation (data-only writes)')
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('info')
    for command in ('write', 'verify'):
        p = sub.add_parser(command); p.add_argument('address', type=lambda s:int(s,0)); p.add_argument('file')
        if command=='write':p.add_argument('--force',action='store_true',help='also erase/program unchanged sectors')
    p = sub.add_parser('package'); p.add_argument('file');p.add_argument('--force',action='store_true',help='also erase/program unchanged sectors')
    p = sub.add_parser('restore'); p.add_argument('directory')
    args = ap.parse_args()
    chunks = None
    if args.command in ('write','verify'):
        chunks = validate_chunks([(args.address, Path(args.file).read_bytes())])
    if args.command == 'package':
        chunks, names = read_package(args.file)
        print('Flash images: '+', '.join(names))
    d, h = connect(args.speed, args.serial)
    was_halted = h.halted()
    flash = Flash(h, args.journal_dir, print)
    success = False
    try:
        h.halt()
        print('JEDEC 0x%06x, 4 MiB, sector 4096' % flash.identify())
        if args.command == 'info': print('SR1/SR2:', '%02x %02x' % flash.status())
        elif args.command == 'verify':
            for a, data in chunks:
                if flash.read(a,len(data)) != data: raise DebugError('verify mismatch at 0x%x'%a)
            print('verify: PASS')
        elif args.command == 'restore': print(flash.restore(args.directory))
        else: print(flash.apply(chunks,force=args.force))
        success = True
    finally:
        try:
            if success and not was_halted and (args.resume or args.command in ('info','verify')):
                h.resume()
            else:
                h.restore_registers()
                print('target left halted; reset before running a changed firmware image')
        finally: d.close()


if __name__ == '__main__':
    try: main()
    except (DebugError, OSError, ValueError) as error: sys.exit(str(error))
