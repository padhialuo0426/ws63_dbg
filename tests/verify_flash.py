#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""Compare the six WS63 v1.0.102 firmware partitions with a full 4 MiB XIP dump."""
import argparse
import hashlib
import json
from pathlib import Path

PARTITIONS = (
    ('param_bin/root_params_sign.bin', 0x200000),
    ('boot_bin/ssb_sign.bin', 0x202000),
    ('boot_bin/flashboot_backup_sign.bin', 0x210000),
    ('boot_bin/flashboot_sign.bin', 0x220000),
    ('ws63-liteos-app/ws63-liteos-app-sign.bin', 0x230000),
    ('nv_bin/ws63_all_nv.bin', 0x5FC000),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sdk', type=Path)
    parser.add_argument('dump', type=Path)
    args = parser.parse_args()
    flash = args.dump.read_bytes()
    if len(flash) != 0x400000:
        parser.error('expected a full 4194304-byte Flash dump')
    partitions = []
    for name, addr in PARTITIONS:
        image = (args.sdk / 'output/ws63/acore' / name).read_bytes()
        offset = addr - 0x200000
        partitions.append({'image': name, 'address': hex(addr), 'length': len(image),
                           'sha256': hashlib.sha256(image).hexdigest(),
                           'matches_flash': bool(image) and flash[offset:offset + len(image)] == image})
    print(json.dumps({'flash_size': len(flash), 'flash_sha256': hashlib.sha256(flash).hexdigest(),
                      'partitions': partitions}, indent=2))
    return 0 if all(p['matches_flash'] for p in partitions) else 1


if __name__ == '__main__':
    raise SystemExit(main())
