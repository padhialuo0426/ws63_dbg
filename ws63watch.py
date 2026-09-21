# SPDX-License-Identifier: GPL-2.0-only
"""WS63 scalar memory-access decoding and transactional data-watchpoint allocation.

The short byte/halfword encodings are WS63 instructions, not standard Zcb.
Unknown instructions never justify automatically resuming the target.
"""
from dataclasses import dataclass
from ws63dbg import DebugError


@dataclass(frozen=True)
class Access:
    address: int
    size: int
    kind: str


def decode_access(code, register):
    """Decode one scalar RV32/WS63 load or store using pre-instruction GPRs.

    Return None for unknown, truncated, misaligned, or wrapping accesses. Reads
    no data memory: even peripheral watchpoints have no additional read effects.
    """
    if len(code) < 2:
        return None
    half = int.from_bytes(code[:2], 'little')
    quad, funct = half & 3, half >> 13
    if quad == 3:
        if half & 31 == 31 or len(code) < 4:
            return None
        insn = int.from_bytes(code[:4], 'little')
        opcode, funct = insn & 127, (insn >> 12) & 7
        if opcode == 3 and funct in (0, 1, 2, 4, 5):
            size, kind = 1 << (funct & 3), 'load'
        elif opcode == 0x23 and funct in (0, 1, 2):
            size, kind = 1 << funct, 'store'
        elif opcode in (7, 0x27) and funct == 2:
            size, kind = 4, 'load' if opcode == 7 else 'store'
        else:
            return None
        base = (insn >> 15) & 31
        offset = insn >> 20 if kind == 'load' else ((insn >> 7) & 31) | ((insn >> 25) << 5)
        if offset & 0x800:
            offset -= 0x1000
    elif quad in (0, 2) and funct in (1, 5):
        # WS63: lbu/sb (quadrant 0), lhu/sh (quadrant 2).
        base = 8 + ((half >> 7) & 7)
        size, kind = (1 if quad == 0 else 2), ('load' if funct == 1 else 'store')
        offset = ((half >> 5) & 3) * 2 + ((half >> 10) & 3) * 8
        offset += ((half >> 12) & 1) * (1 if size == 1 else 32)
    elif quad == 0 and funct in (2, 3, 6, 7):
        base, size = 8 + ((half >> 7) & 7), 4
        kind = 'load' if funct in (2, 3) else 'store'
        offset = ((half >> 6) & 1) * 4 + ((half >> 10) & 7) * 8 + ((half >> 5) & 1) * 64
    elif quad == 2 and funct in (2, 3, 6, 7):
        base, size = 2, 4
        kind = 'load' if funct in (2, 3) else 'store'
        if kind == 'load':
            if funct == 2 and (half >> 7) & 31 == 0:
                return None  # reserved C.LWSP rd=x0
            offset = ((half >> 4) & 7) * 4 + ((half >> 12) & 1) * 32 + ((half >> 2) & 3) * 64
        else:
            offset = ((half >> 9) & 15) * 4 + ((half >> 7) & 3) * 64
    else:
        return None
    address = ((register(base) if base else 0) + offset) & 0xffffffff
    if address % size or address + size > 0x100000000:
        return None
    return Access(address, size, kind)


def range_blocks(address, length):
    """Cover the watched bytes with aligned power-of-two blocks of >=4 bytes.

    Four-byte guards catch naturally aligned word/halfword operations starting
    before a watched byte. The caller must filter non-overlapping scalar accesses.
    """
    if length <= 0 or address < 0 or address + length > 0x100000000:
        raise ValueError('watchpoint range must fit the 32-bit address space')
    end = (address + length + 3) & ~3
    current = address & ~3
    blocks = []
    while current < end:
        size = 1 << ((end-current).bit_length()-1)
        if current:
            size = min(size, current & -current)
        blocks.append((current, size))
        current += size
    return blocks


@dataclass(frozen=True)
class Watchpoint:
    address: int
    length: int
    kind: str
    blocks: tuple  # (hardware slot, aligned address, size)

    def matches(self, access):
        return (self.kind in (access.kind, 'access') and
                self.address < access.address + access.size and
                access.address < self.address + self.length)

    def guards(self, access):
        return (self.kind in (access.kind, 'access') and
                any(a <= access.address < a+n for _, a, n in self.blocks))


class Watchpoints:
    def __init__(self, hart):
        self.h = hart
        self.entries = {}
        self.blocked = set()

    @property
    def unsafe(self):
        return bool(self.blocked)

    def slots(self):
        return [i for entry in self.entries.values() for i, _, _ in entry.blocks]

    def insert(self, address, kind, length, reserved=()):
        if kind not in ('load', 'store', 'access'):
            raise ValueError('invalid watchpoint kind')
        blocks = range_blocks(address, length)
        key = (address, kind, length)
        if key in self.entries:
            if key in self.blocked:
                raise DebugError('watchpoint setup failed; remove it before retrying')
            return True
        used = set(reserved) | set(self.slots())
        free = [i for i in range(self.h.count_triggers()) if i not in used]
        if len(free) < len(blocks):
            return False
        entry = Watchpoint(address, length, kind, tuple((i, a, n) for i, (a, n) in zip(free, blocks)))
        # Keep ownership even when a failed transfer leaves hardware uncertain.
        self.entries[key] = entry
        try:
            for i, a, n in entry.blocks:
                self.h.set_trigger(i, a, kind, n)
        except BaseException:
            self.blocked.add(key)
            self.remove(address, kind, length)
            raise
        return True

    def remove(self, address, kind, length):
        key = (address, kind, length)
        entry = self.entries.get(key)
        if entry is None:
            return
        try:
            for i, _, _ in entry.blocks:
                self.h.clear_trigger(i)
        except BaseException:
            self.blocked.add(key)
            raise
        del self.entries[key]
        self.blocked.discard(key)

    def clear(self):
        for key in list(self.entries):
            self.remove(*key)

    def enable(self):
        for key, entry in self.entries.items():
            try:
                for i, a, n in entry.blocks:
                    self.h.set_trigger(i, a, entry.kind, n)
            except BaseException:
                self.blocked.add(key)
                raise

    def disable(self):
        for key, entry in self.entries.items():
            try:
                for i, _, _ in entry.blocks:
                    self.h.clear_trigger(i)
            except BaseException:
                self.blocked.add(key)
                raise

    def classify(self, pc, read_code):
        code = read_code(pc, 2)
        if len(code) == 2 and code[0] & 3 == 3 and code[0] & 31 != 31:
            code += read_code(pc+2, 2)
        access = decode_access(code, self.h.read_gpr)
        if access is None:
            return None, [], False
        entries = list(self.entries.values())
        return access, [e for e in entries if e.matches(access)], any(e.guards(access) for e in entries)
