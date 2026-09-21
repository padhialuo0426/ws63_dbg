# SPDX-License-Identifier: GPL-2.0-only
"""Software instruction breakpoints with memory shadowing and cache maintenance."""
from contextlib import contextmanager
from ws63dbg import CSR_DCSR, DebugError, FLASH_BASE, FLASH_END


class SoftwareBreakpoints:
    def __init__(self,hart,flash=None):
        self.h=hart;self.flash=flash;self.entries={};self.ebreakm=None

    def _write(self,address,data):
        if FLASH_BASE<=address<FLASH_END:
            if self.flash is None:raise DebugError('Flash software breakpoints are disabled')
            self.flash.apply([(address,data)])
        else:self.h.write_mem(address,data)
        self.h.sync_code()
        if self.h.read_mem(address,len(data))!=data:raise DebugError('instruction patch verification failed')

    def insert(self,address,length):
        if address in self.entries:
            if len(self.entries[address][0])!=length:raise DebugError('conflicting software breakpoint length')
            return
        if length not in (2,4) or address%2:raise DebugError('software breakpoint must be aligned, length 2 or 4')
        if not (0x14c000<=address<address+length<=0x180000 or 0xa00000<=address<address+length<=0xa88000 or
                self.flash is not None and FLASH_BASE<=address<address+length<=FLASH_END):
            raise DebugError('not a supported executable memory region')
        if any(address<a+len(old) and address+length>a for a,(old,_) in self.entries.items()):
            raise DebugError('overlapping software breakpoints')
        old=self.h.read_mem(address,length);patch=b'\x02\x90' if length==2 else b'\x73\x00\x10\x00'
        if self.ebreakm is None:self.ebreakm=self.h.read_csr(CSR_DCSR)&0x8000
        self.h.write_csr(CSR_DCSR,self.h.read_csr(CSR_DCSR)|0x8000)
        # Register first so a failed write is still cleaned up by the caller.
        self.entries[address]=(old,patch)
        try:self._write(address,patch)
        except BaseException:
            if self.h.read_mem(address,length)!=old:self._write(address,old)
            del self.entries[address]
            self._restore_dcsr()
            raise

    def _restore_dcsr(self):
        if not self.entries and self.ebreakm is not None:
            self.h.write_csr(CSR_DCSR,(self.h.read_csr(CSR_DCSR)&~0x8000)|self.ebreakm)
            self.ebreakm=None

    def remove(self,address):
        if address not in self.entries:return
        old,patch=self.entries[address]
        actual=self.h.read_mem(address,len(patch))
        if actual not in (old,patch):raise DebugError('breakpoint code changed; refusing to overwrite at 0x%x'%address)
        self._write(address,old)
        del self.entries[address];self._restore_dcsr()

    def clear(self):
        for address in list(self.entries):self.remove(address)

    def read(self,address,length):
        data=bytearray(self.h.read_mem(address,length))
        for a,(old,_) in self.entries.items():
            start,end=max(a,address),min(a+len(old),address+length)
            if start<end:data[start-address:end-address]=old[start-a:end-a]
        return bytes(data)

    def write(self,address,data):
        physical=bytearray(data);changed={}
        for a,(old,patch) in self.entries.items():
            start,end=max(a,address),min(a+len(old),address+len(data))
            if start<end:
                new=bytearray(old);new[start-a:end-a]=data[start-address:end-address]
                changed[a]=(bytes(new),patch);physical[start-address:end-address]=patch[start-a:end-a]
        self.h.write_mem(address,bytes(physical))
        self.entries.update(changed)
        if changed:self.h.sync_code()

    @contextmanager
    def displaced(self,address):
        entry=self.entries.get(address)
        if entry:self._write(address,entry[0])
        try:yield
        finally:
            if entry:self._write(address,entry[1])

    def reset(self):
        # Reset may replace RAM code; never write old instructions over a new image.
        kept={a:e for a,e in self.entries.items() if FLASH_BASE<=a<FLASH_END}
        count=len(self.entries)-len(kept);self.entries=kept;self.ebreakm=None
        if kept:
            self.ebreakm=self.h.read_csr(CSR_DCSR)&0x8000
            self.h.write_csr(CSR_DCSR,self.h.read_csr(CSR_DCSR)|0x8000)
        return count
