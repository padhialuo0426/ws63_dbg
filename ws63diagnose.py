#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Capture a halted WS63 and inspect the snapshot without touching hardware."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from ws63dbg import DebugError


def capture(hart, image, directory, rtos=None, ranges=None):
    if not hart.halted():raise DebugError('snapshot requires a halted target')
    if rtos:rtos.refresh()
    elif image:image.check_target(hart)
    root=Path(directory);root.mkdir(parents=True,exist_ok=False)
    registers={str(i):hart.read_gpr(i) for i in range(32)}
    registers['32']=hart.pc();errors=[]
    for csr in (0x300,0x301,0x304,0x305,0x340,0x341,0x342,0x343,0x344,0x7b0,0x7b1):
        try:registers[str(65+csr)]=hart.read_csr(csr)
        except DebugError as error:errors.append(dict(csr=hex(csr),error=str(error)))
    for i in range(32):
        try:registers[str(33+i)]=hart.read_fpr(i)
        except DebugError:break
    for csr in (1,2,3):
        try:registers[str(65+csr)]=hart.read_csr(csr)
        except DebugError:pass
    if ranges is None:ranges=[(0xa00000,0x88000),(0x180000,0x8000),(0x14c000,0x4000)]
    segments=[]
    for address,size in ranges:
        if size<=0 or address<0 or address+size>0x100000000:raise DebugError('invalid snapshot range')
        for a in range(address,address+size,4096):
            n=min(4096,address+size-a)
            try:data=hart.read_mem(a,n)
            except DebugError as error:
                errors.append(dict(address=a,size=n,error=str(error)));continue
            name='%08x.bin'%a;(root/name).write_bytes(data)
            segments.append(dict(address=a,size=n,file=name,sha256=hashlib.sha256(data).hexdigest()))
    result=dict(version=1,created=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                elf_sha256=image.sha256 if image else None,registers=registers,segments=segments,errors=errors,
                tasks=[],current=rtos.current if rtos else 1,interrupt_depth=rtos.interrupt_depth if rtos else None)
    if rtos:
        for task in rtos.tasks.values():
            record=asdict(task)
            try:record['waterline']=rtos.waterline(task)
            except DebugError as error:record['waterline']={'error':str(error)}
            try:record['backtrace'],record['unwind_stop']=rtos.backtrace(task)
            except DebugError as error:record['backtrace'],record['unwind_stop']=[],str(error)
            result['tasks'].append(record)
        result['synchronization']=rtos.synchronization()
    (root/'snapshot.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return root


class FrozenHart:
    """Read-only snapshot memory. Missing bytes are errors, never zero-filled."""
    def __init__(self,directory,image=None):
        root=Path(directory);self.meta=json.loads((root/'snapshot.json').read_text())
        if self.meta.get('version')!=1:raise DebugError('unsupported snapshot')
        self.dap=SimpleNamespace(name='offline snapshot')
        self.regs={int(k):v for k,v in self.meta['registers'].items()};self.regions=[]
        for segment in self.meta['segments']:
            name=segment['file']
            if Path(name).name!=name:raise DebugError('invalid snapshot path')
            data=(root/name).read_bytes()
            if len(data)!=segment['size'] or hashlib.sha256(data).hexdigest()!=segment['sha256']:
                raise DebugError('corrupt snapshot segment '+name)
            self.regions.append((segment['address'],data))
        if image:
            if image.sha256!=self.meta['elf_sha256']:raise DebugError('snapshot ELF hash mismatch')
            # These Flash executable bytes were validated during capture.
            for sec in image.elf.iter_sections():
                a=sec['sh_addr'];size=sec['sh_size']
                if sec['sh_flags']&6==6 and 0x200000<=a<a+size<=0x600000:
                    self.regions.append((a,sec.data()))
        self.regions.sort()

    def read_mem(self,address,size):
        if size<0 or address<0 or address+size>0x100000000:raise DebugError('invalid memory range')
        out=bytearray()
        while size:
            for start,data in self.regions:
                if start<=address<start+len(data):
                    n=min(size,start+len(data)-address);out+=data[address-start:address-start+n]
                    address+=n;size-=n;break
            else:raise DebugError('snapshot has no memory at 0x%x'%address)
        return bytes(out)

    def read_gpr(self,n):
        if n not in self.regs:raise DebugError('register unavailable')
        return self.regs[n]
    def pc(self):return self.regs[32]
    def read_csr(self,n):return self.read_gpr(65+n)
    def read_fpr(self,n):return self.read_gpr(33+n)
    def halted(self):return True
    def halt(self):pass
    def halt_cause(self):return (self.regs.get(65+0x7b0,0)>>6)&7
    def had_reset(self):return False
    def restore_registers(self):pass
    def count_triggers(self):return 0
    def close(self):pass


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('capture');p.add_argument('directory');p.add_argument('--elf',required=True);p.add_argument('--serial');p.add_argument('--speed',type=int,default=4000)
    p=sub.add_parser('report');p.add_argument('directory')
    p=sub.add_parser('serve');p.add_argument('directory');p.add_argument('--elf',required=True);p.add_argument('--port',type=int,default=3333)
    args=ap.parse_args()
    if args.command=='report':
        print((Path(args.directory)/'snapshot.json').read_text());return
    from ws63elf import Image
    from ws63rtos import LiteOS
    image=Image(args.elf)
    if args.command=='serve':
        from gdbserver import Server
        hart=FrozenHart(args.directory,image);rtos=LiteOS(hart,image)
        from ws63rtos import Task
        for record in hart.meta['tasks']:
            values={k:v for k,v in record.items() if k in Task.__dataclass_fields__}
            values['registers']={int(k):v for k,v in values['registers'].items()}
            task=Task(**values);rtos.tasks[task.thread]=task
        rtos.current=hart.meta['current'];rtos.selected=rtos.current
        rtos.interrupt_depth=hart.meta.get('interrupt_depth') or 0
        rtos.verified=True;rtos.valid=True;rtos.frozen=True
        Server(hart,rtos=rtos,image=image,readonly=True).serve(args.port);return
    from ws63dbg import connect
    dap,hart=connect(args.speed,args.serial);was=hart.halted()
    try:
        hart.halt();rtos=LiteOS(hart,image)
        print(capture(hart,image,args.directory,rtos))
    finally:
        if not was:hart.resume()
        else:hart.restore_registers()
        dap.close()


if __name__=='__main__':main()
