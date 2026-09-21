#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify negotiated CMSIS-DAP v2 packet boundaries and repeated WS63 reads.

Uses harmless DP SELECT writes and reads ROM/Flash; no halt, reset or Flash write.
Requires exclusive probe access and an enabled WS63 SWD port.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmsisdap import BulkTransport
import ws63dbg as w


def check(probe, hart):
    largest = {'sent':0, 'received':0}
    send, receive = probe.t.send, probe.t.recv

    def measured_send(data):
        largest['sent'] = max(largest['sent'], len(data))
        return send(data)

    def measured_receive():
        data = receive()
        largest['received'] = max(largest['received'], len(data))
        return data

    probe.t.send, probe.t.recv = measured_send, measured_receive
    try:
        # Fill one complete command with idempotent SELECT writes and ID reads.
        probe.select(w.AP_DM)
        writes, reads = divmod(probe.packet_size-3, 5)
        expected_id = probe.dpidr()
        values = probe.transfer([(2, w.DP, w.AP_DM << 24)]*writes + [(0, w.DP, None)]*reads)
        assert values == [expected_id]*reads
        assert largest['sent'] == probe.packet_size
        print('full-length bulk OUT command: PASS', flush=True)

        for name, address, size in [('ROM', 0x100000, 0x4c000), ('Flash app prefix', 0x230000, 0x10000)]:
            start = time.monotonic()
            first = hart.read_mem(address, size)
            elapsed = time.monotonic()-start
            second = hart.read_mem(address, size)
            assert len(first) == size and first == second
            print(json.dumps({'region':name, 'bytes':size, 'read_seconds':elapsed,
                              'sha256':hashlib.sha256(first).hexdigest(), 'repeat_equal':True}), flush=True)
        assert largest['received'] == probe.packet_size
        print('full-length / short / pipelined bulk IN responses: PASS', flush=True)
        print('largest USB transfers: OUT=%d IN=%d bytes' % (largest['sent'], largest['received']), flush=True)
    finally:
        probe.t.send, probe.t.recv = send, receive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--speed', type=int, default=4000)
    parser.add_argument('--expect-packet-size', type=int, choices=(64, 512))
    args = parser.parse_args()
    probe, hart = w.connect(args.speed, args.serial)
    try:
        if not isinstance(probe.t, BulkTransport):
            raise w.DebugError('requires a CMSIS-DAP v2 bulk probe')
        if args.expect_packet_size is not None and probe.packet_size != args.expect_packet_size:
            raise w.DebugError('probe reports %d-byte packets, expected %d' % (probe.packet_size, args.expect_packet_size))
        print('%s: packet_size=%d, packet_count=%d, SWD=%d kHz' %
              (probe.name, probe.packet_size, probe.packet_count, probe.speed), flush=True)
        check(probe, hart)
        print('target state: '+('halted' if hart.halted() else 'running'), flush=True)
    finally:
        probe.close()


if __name__ == '__main__':
    main()
