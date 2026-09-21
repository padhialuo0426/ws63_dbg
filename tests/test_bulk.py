# SPDX-License-Identifier: GPL-2.0-only
"""USB response-boundary regressions; no probe is opened."""
from collections import deque
import ctypes
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cmsisdap as c
from ws63dbg import AP, DebugError


class BulkEndpoint:
    """USB reads finish at a short packet, requested length, or timeout."""
    def __init__(self, packets, usb_packet_size=512):
        self.packets = deque(packets)
        self.usb_packet_size = usb_packet_size
        self.read_lengths = []

    def libusb_bulk_transfer(self, handle, endpoint, buffer, length, transferred, timeout):
        if endpoint & 0x80:
            self.read_lengths.append(length)
            data = bytearray()
            status = -7
            while self.packets and len(data) < length:
                packet = self.packets.popleft()
                if len(data)+len(packet) > length:
                    raise AssertionError('test received a USB packet larger than the buffer')
                data.extend(packet)
                if len(packet) < self.usb_packet_size or len(data) == length:
                    status = 0
                    break
            ctypes.memmove(buffer, bytes(data), len(data))
            count = len(data)
        else:
            count, status = length, 0
        ctypes.cast(transferred, ctypes.POINTER(ctypes.c_int)).contents.value = count
        return status


def transport(packets, packet_size=512, usb_packet_size=512):
    t = c.BulkTransport.__new__(c.BulkTransport)
    t.lib = BulkEndpoint(packets, usb_packet_size)
    t.handle = None
    t.info = {'ep_in':0x81, 'ep_out':0x02}
    t.report_size = None
    t.packet_size = packet_size
    return t


class BulkTests(unittest.TestCase):
    def test_single_full_response_returns_without_waiting_for_another(self):
        reply = b'\x06'+bytes(511)
        self.assertEqual(transport([reply]).recv(), reply)

    def test_pipelined_full_and_short_block_replies_stay_separate(self):
        expected = list(range(256))
        packets = [struct.pack('<BHB', c.DAP_TRANSFER_BLOCK, len(values), c.ACK_OK) +
                   struct.pack('<%dI' % len(values), *values)
                   for values in (expected[:127], expected[127:254], expected[254:])]
        dap = c.CMSISDAP.__new__(c.CMSISDAP)
        dap.packet_size, dap.packet_count = 512, 4
        dap.t = transport(packets)
        self.assertEqual(dap.rd_repeat(3, AP, len(expected)), expected)
        self.assertFalse(dap.t.lib.packets)

    def test_dap_info_configures_64_and_512_byte_receive_limits(self):
        for size in (64, 512):
            with self.subTest(size=size):
                t = transport([])
                info = {'transport':'bulk', 'product':'test'}
                replies = [struct.pack('<H', size), b'\x04', b'\x01', b'2.1.0\0']
                with patch.object(c, 'find_probes', return_value=[info]), \
                     patch.object(c, 'BulkTransport', return_value=t), \
                     patch.object(c.CMSISDAP, 'info', side_effect=replies), \
                     patch.object(c.CMSISDAP, '_init_swd'):
                    dap = c.CMSISDAP()
                reply = b'\x06'+bytes(size-1)
                t.lib.packets.extend([reply, reply])
                self.assertEqual(dap.t.recv(), reply)
                self.assertEqual(dap.t.recv(), reply)
                self.assertEqual(t.lib.read_lengths, [size, size])

    def test_timeout_after_partial_data_is_not_accepted_as_a_response(self):
        t = transport([b'\x06'+bytes(63)], usb_packet_size=64)
        with self.assertRaisesRegex(DebugError, 'bulk read failed'):
            t.recv()


if __name__ == '__main__':
    unittest.main()
