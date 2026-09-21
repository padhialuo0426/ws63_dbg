#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare all short scalar-memory encodings with the WS63 SDK objdump."""
import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ws63elf import REGNUM
from ws63watch import Access, decode_access


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('objdump', help='WS63 SDK riscv32-linux-musl-objdump path')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory)/'short.bin'
        binary.write_bytes(b''.join(i.to_bytes(2, 'little') for i in range(65536) if i & 3 != 3))
        result = subprocess.run([args.objdump, '-D', '-b', 'binary', '-m', 'riscv:rv32', str(binary)],
                                text=True, capture_output=True, check=True, timeout=30)
    seen, checked = set(), Counter()
    sizes = {'lbu':1, 'lhu':2, 'lw':4, 'flw':4, 'sb':1, 'sh':2, 'sw':4, 'fsw':4}
    for line in result.stdout.splitlines():
        match = re.match(r'\s*[0-9a-f]+:\s+([0-9a-f]{4})\s+(\w+)\s+[^,]+,(\d+)\(([^)]+)\)', line)
        if not match or match[2] not in sizes:
            continue
        raw, op, offset, base = match.groups()
        instruction = int(raw, 16)
        actual = decode_access(instruction.to_bytes(2, 'little'), lambda n: n*0x1000)
        expected = Access(REGNUM[base]*0x1000+int(offset), sizes[op],
                          'load' if op in ('lbu', 'lhu', 'lw', 'flw') else 'store')
        if actual != expected:
            raise AssertionError('%s: %s != %s' % (line.strip(), actual, expected))
        seen.add(instruction)
        checked[op] += 1
    for instruction in range(65536):
        if instruction & 3 != 3 and decode_access(instruction.to_bytes(2, 'little'), lambda n:n*0x1000):
            if instruction not in seen:
                raise AssertionError('decoder accepted unknown SDK instruction 0x%04x' % instruction)
    if set(checked) != set(sizes) or sum(checked.values()) != 24512:
        raise AssertionError('unexpected SDK opcode coverage: %s' % checked)
    print('24,512 WS63 scalar-memory encodings match SDK objdump; no extra encodings accepted: PASS')


if __name__ == '__main__':
    main()
