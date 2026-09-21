# SPDX-License-Identifier: GPL-2.0-only
"""Read-only LiteOS inspection using symbols and structure layouts from an ELF."""
from dataclasses import dataclass, field
from ws63dbg import DebugError
from ws63elf import REGS


@dataclass
class Task:
    thread: int
    ident: int
    tcb: int
    name: str
    status: int
    priority: int
    low: int
    size: int
    saved_sp: int
    entry: int
    current: bool
    registers: dict = field(default_factory=dict)
    objects: dict = field(default_factory=dict)
    error: str = ''


class LiteOS:
    def __init__(self, hart, image):
        self.h=hart;self.image=image;self.tasks={};self.current=1;self.interrupt_depth=0
        self.valid=False;self.verified=False;self.selected=1;self.identities={};self.next_thread=2;self.frozen=False
        self.tsize,self.fields=image.type_layout('LosTaskCB')
        self.csize,self.context=image.type_layout('TaskContext')
        for name in ('stackPointer','taskStatus','priority','stackSize','topOfStack','taskId','taskEntry','taskName'):
            if name not in self.fields:raise DebugError('unsupported LosTaskCB: missing '+name)
        for name in ('mepc','mstatus','s0','s1',*['s%d'%i for i in range(2,12)]):
            if name not in self.context:raise DebugError('unsupported TaskContext: missing '+name)
        if not 32<=self.tsize<=1024 or self.csize not in (128,272):raise DebugError('unsupported LiteOS layout')
        for name in ('g_osTaskCBArray','g_taskMaxNum','g_newTask'):image.symbol(name)

    def invalidate(self, reset=False):
        if self.frozen:return
        self.valid=False
        if reset:self.verified=False;self.tasks={};self.current=1;self.selected=1

    def u32(self,a):return int.from_bytes(self.h.read_mem(a,4),'little')

    @staticmethod
    def ram(a,n=1):return n>=0 and ((0xa00000<=a<=a+n<=0xa88000) or (0x180000<=a<=a+n<=0x1c8000))

    def field(self,data,name):
        off,size=self.fields[name]
        if size not in (1,2,4) or off+size>len(data):raise DebugError('unsupported scalar field '+name)
        return int.from_bytes(data[off:off+size],'little')

    def refresh(self):
        if self.valid:return
        if not self.h.halted():raise DebugError('task inspection requires halt')
        if self.h.pc()==0x100000:
            # Warm reset can leave old TCBs and stack registers in RAM. Do not
            # report them as live tasks before the reset vector has executed.
            self.tasks={};self.current=1;self.selected=1;self.valid=True
            return
        if not self.verified:
            self.image.check_target(self.h);self.verified=True
        array=self.u32(self.image.symbol('g_osTaskCBArray'))
        count=self.u32(self.image.symbol('g_taskMaxNum'))
        current=self.u32(self.image.symbol('g_newTask'))
        self.tasks={};self.current=1
        scheduled=self.image.symbols.get('g_taskScheduled')
        if count==0 or array==0 or scheduled is not None and self.u32(scheduled)==0:
            self.valid=True;self.selected=1;return
        if not 1<=count<=512 or not self.ram(array,count*self.tsize):
            raise DebugError('invalid task array; wrong ELF or kernel not initialized')
        self.interrupt_depth=self.u32(self.image.symbols['g_intCount']) if 'g_intCount' in self.image.symbols else 0
        present={}
        for i in range(count):
            a=array+i*self.tsize;raw=self.h.read_mem(a,self.tsize)
            get=lambda name:self.field(raw,name)
            status=get('taskStatus')
            if status&1 or status==0:continue
            low,size,sp=get('topOfStack'),get('stackSize'),get('stackPointer')
            entry,ident=get('taskEntry'),get('taskId')
            nameptr=get('taskName')
            name='invalid-name'
            if self.ram(nameptr,64) or 0x100000<=nameptr<0x600000-64:
                name=self.h.read_mem(nameptr,64).split(b'\0')[0].decode('utf-8','replace')
            key=(a,ident,low,size,entry,name)
            thread=self.identities.get(key)
            if thread is None:thread=self.next_thread;self.next_thread+=1
            present[key]=thread
            task=Task(thread,ident,a,name,status,get('priority'),low,size,sp,entry,a==current)
            task.objects={n:get(n) for n in ('taskMux','taskSem') if n in self.fields}
            if not self.ram(low,size) or not 128<=size<=1024*1024:
                task.error='invalid stack range'
            elif task.current:
                if not low<=self.h.read_gpr(2)<=low+size:
                    task.current=False
                    task.error='live SP outside task stack (interrupt/scheduler transition)'
                else:
                    task.registers={i:self.h.read_gpr(i) for i in range(32)};task.registers[32]=self.h.pc()
                    self.current=thread
            elif not low<=sp<=sp+self.csize<=low+size:
                task.error='saved context outside stack'
            else:
                context=self.h.read_mem(sp,self.csize)
                def reg(name):
                    off,n=self.context[name]
                    return int.from_bytes(context[off:off+n],'little')
                task.registers={0:0,2:sp+self.csize,32:reg('mepc'),65+0x300:reg('mstatus')}
                # Cooperative switches preserve these; caller-saved registers may be stale.
                for i in [8,9,*range(18,28)]:task.registers[i]=reg(REGS[i])
                if self.csize==272:
                    for i in [8,9,*range(18,28)]:
                        name='fs'+str([8,9,*range(18,28)].index(i))
                        if name in self.context:task.registers[33+i]=reg(name)
                    if 'fcsr' in self.context:task.registers[68]=reg('fcsr')
            self.tasks[thread]=task
        self.identities=present
        if self.selected not in self.tasks:self.selected=self.current
        self.valid=True

    def threads(self):
        self.refresh();return list(self.tasks) if self.current!=1 else [1]+list(self.tasks)

    def select(self,thread):
        self.refresh()
        if thread in (0,-1):thread=self.current
        if thread not in self.threads():raise DebugError('unknown thread')
        self.selected=thread

    def live_selected(self):
        self.refresh();return self.selected==self.current

    def extra(self,thread):
        self.refresh()
        if thread==1:return 'boot/interrupt context (no current LiteOS task)'
        t=self.tasks[thread]
        return '%s task=%d priority=%d state=0x%x%s%s'%(t.name,t.ident,t.priority,t.status,
            ' ISR depth=%d'%self.interrupt_depth if t.current and self.interrupt_depth else '',
            ' '+t.error if t.error else '')

    def waterline(self,task):
        if task.error:return dict(error=task.error)
        data=self.h.read_mem(task.low,task.size)
        if data[:4]!=b'\xcc'*4:return dict(magic_ok=False,free_min=None,used_max=None)
        free=0
        for value in data[4:]:
            if value!=0xca:break
            free+=1
        return dict(magic_ok=True,free_min=free,used_max=task.size-4-free)

    def backtrace(self,task,limit=32):
        regs=dict(task.registers);frames=[];seen=set();reason='frame limit'
        if task.error:return [],task.error
        for depth in range(limit):
            pc=regs.get(32);sp=regs.get(2)
            if pc is None or sp is None:reason='register unavailable';break
            if (pc,sp) in seen:reason='unwind cycle';break
            seen.add((pc,sp))
            frames.append(dict(pc=pc,sp=sp,symbol=self.image.describe(pc if depth==0 else max(0,pc-1))))
            if self.image.describe(pc).startswith('OsTaskEntry'):
                reason='task entry';break
            fde=self.image.fde(pc if depth==0 else max(0,pc-1))
            if fde is None:reason='no CFI (prepare a debug ELF with ws63elf.py)';break
            rows=fde.get_decoded().table
            matches=[r for r in rows if r['pc']<=(pc if depth==0 else pc-1)]
            if not matches:reason='no CFI row';break
            row=matches[-1];cfa=row['cfa']
            if cfa.expr is not None or cfa.reg not in regs:reason='unsupported/unavailable CFA';break
            top=regs[cfa.reg]+cfa.offset
            if not task.low<=sp<=top<=task.low+task.size:reason='CFA outside stack';break
            restored=dict(regs)
            try:
                for reg,rule in row.items():
                    if not isinstance(reg,int):continue
                    if rule.type=='OFFSET':
                        a=top+rule.arg
                        if not task.low<=a<=a+4<=task.low+task.size:raise DebugError('saved register outside stack')
                        restored[reg]=self.u32(a)
                    elif rule.type=='UNDEFINED':restored.pop(reg,None)
                    elif rule.type=='REGISTER':
                        if rule.arg in regs:restored[reg]=regs[rule.arg]
                        else:restored.pop(reg,None)
                    elif rule.type!='SAME_VALUE':raise DebugError('unsupported CFI rule '+rule.type)
            except DebugError as error:reason=str(error);break
            restored[2]=top
            restored[32]=restored.get(fde.cie['return_address_register'])
            # Caller-saved registers do not retain useful values between frames.
            for r in [5,6,7,*range(10,18),28,29,30,31]:restored.pop(r,None)
            regs=restored
        return frames,reason

    def synchronization(self):
        self.refresh();result=[];owners={t.tcb:t for t in self.tasks.values()};edges={}
        for t in self.tasks.values():
            for name,ptr in t.objects.items():
                if not ptr:continue
                typ='LosMuxCB' if name=='taskMux' else 'LosSemCB'
                try:
                    size,fields=self.image.type_layout(typ)
                    if not self.ram(ptr,size):raise DebugError('invalid object pointer')
                    raw=self.h.read_mem(ptr,size)
                    read=lambda key:int.from_bytes(raw[fields[key][0]:fields[key][0]+fields[key][1]],'little')
                    record=dict(task=t.ident,name=t.name,object=hex(ptr),kind=typ)
                    if name=='taskMux':
                        owner=owners.get(read('owner'))
                        record.update(owner=owner.ident if owner else None,count=read('muxCount'),state=read('muxStat'))
                        if owner:edges[t.ident]=owner.ident
                    else:record.update(count=read('semCount'),state=read('semStat'))
                    result.append(record)
                except (DebugError,KeyError) as error:result.append(dict(task=t.ident,object=hex(ptr),error=str(error)))
        cycles=[]
        for start in edges:
            chain=[];node=start
            while node in edges and node not in chain:chain.append(node);node=edges[node]
            if node in chain:
                cycle=chain[chain.index(node):]
                key=tuple(sorted(cycle))
                if key not in [tuple(sorted(c)) for c in cycles]:cycles.append(cycle)
        return dict(waiting=result,mutex_wait_cycles=cycles,note='cycles are candidates; snapshot does not prove a permanent deadlock')

    def listing(self,water=False):
        self.refresh();lines=['TASK  THREAD  PRI STATE  STACK USED/MAX  NAME']
        for t in self.tasks.values():
            usage=self.waterline(t) if water else {}
            used=str(usage.get('used_max','?'))
            suffix=' bad stack magic' if usage.get('magic_ok') is False else ''
            lines.append('%4d  %6x  %3d %04x  %s/%d  %s%s%s'%(t.ident,t.thread,t.priority,t.status,used,t.size,
                t.name,' *' if t.current else '',suffix or (' '+t.error if t.error else '')))
        return '\n'.join(lines)+'\n'
