#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# See LICENSE in the project root.
"""CMSIS-DAP probe backend for ws63dbg (no third-party dependencies).

Transports:
  - CMSIS-DAP v2 (USB bulk): through libusb-1.0 via ctypes
  - CMSIS-DAP v1 (USB HID):  through Linux /dev/hidraw*

v2 is preferred when a probe offers both, since bulk transfers have much lower latency.
"""
import ctypes
import ctypes.util
import glob
import os
import select
import struct

from ws63dbg import DAP, DebugError

# CMSIS-DAP commands
DAP_INFO = 0x00
DAP_HOST_STATUS = 0x01
DAP_CONNECT = 0x02
DAP_DISCONNECT = 0x03
DAP_TRANSFER_CONFIGURE = 0x04
DAP_TRANSFER = 0x05
DAP_TRANSFER_BLOCK = 0x06
DAP_WRITE_ABORT = 0x08
DAP_SWJ_CLOCK = 0x11
DAP_SWJ_SEQUENCE = 0x12
DAP_SWD_CONFIGURE = 0x13

INFO_PRODUCT_FW_VERSION = 0x09
INFO_FW_VERSION = 0x04
INFO_CAPABILITIES = 0xF0
INFO_PACKET_COUNT = 0xFE
INFO_PACKET_SIZE = 0xFF

ACK_OK, ACK_WAIT, ACK_FAULT = 1, 2, 4
TIMEOUT_S = 2.0


# --------------------------------------------------------------------------- v1: hidraw

def _hidraw_probes():
    probes = []
    for dev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = dict(l.split("=", 1) for l in open(dev + "/device/uevent").read().split("\n") if "=" in l)
        except OSError:
            continue
        if "CMSIS-DAP" in uevent.get("HID_NAME", ""):
            probes.append({"transport": "hid", "path": "/dev/" + os.path.basename(dev),
                           "product": uevent.get("HID_NAME", ""), "serial": uevent.get("HID_UNIQ", "")})
    return probes


class HidTransport:
    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise DebugError("cannot open %s: %s (check udev permissions)" % (path, e))
        self.report_size = 64
        while True:  # drop stale responses left by a previous session
            try:
                if not os.read(self.fd, 1024):
                    break
            except BlockingIOError:
                break

    def send(self, data):
        # Report ID 0 prefix, payload padded to the full report size.
        packet = b"\x00" + bytes(data).ljust(self.report_size, b"\x00")
        if os.write(self.fd, packet) != len(packet):
            raise DebugError("CMSIS-DAP HID short write")

    def recv(self):
        r, _, _ = select.select([self.fd], [], [], TIMEOUT_S)
        if not r:
            raise DebugError("CMSIS-DAP HID read timed out")
        return os.read(self.fd, 1024)

    def xfer(self, data):
        self.send(data)
        return self.recv()

    def close(self):
        os.close(self.fd)


# --------------------------------------------------------------------------- v2: libusb bulk

class _EndpointDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bEndpointAddress", ctypes.c_uint8), ("bmAttributes", ctypes.c_uint8),
                ("wMaxPacketSize", ctypes.c_uint16), ("bInterval", ctypes.c_uint8),
                ("bRefresh", ctypes.c_uint8), ("bSynchAddress", ctypes.c_uint8),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class _InterfaceDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bInterfaceNumber", ctypes.c_uint8), ("bAlternateSetting", ctypes.c_uint8),
                ("bNumEndpoints", ctypes.c_uint8), ("bInterfaceClass", ctypes.c_uint8),
                ("bInterfaceSubClass", ctypes.c_uint8), ("bInterfaceProtocol", ctypes.c_uint8),
                ("iInterface", ctypes.c_uint8), ("endpoint", ctypes.POINTER(_EndpointDesc)),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class _Interface(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(_InterfaceDesc)), ("num_altsetting", ctypes.c_int)]


class _ConfigDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("wTotalLength", ctypes.c_uint16), ("bNumInterfaces", ctypes.c_uint8),
                ("bConfigurationValue", ctypes.c_uint8), ("iConfiguration", ctypes.c_uint8),
                ("bmAttributes", ctypes.c_uint8), ("MaxPower", ctypes.c_uint8),
                ("interface", ctypes.POINTER(_Interface)),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class _DeviceDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]


_libusb = None
_ctx = None


def _usb():
    global _libusb, _ctx
    if _libusb is None:
        name = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
        try:
            lib = ctypes.cdll.LoadLibrary(name)
        except OSError:
            return None
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
        lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DeviceDesc)]
        lib.libusb_get_active_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_ConfigDesc))]
        lib.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(_ConfigDesc)]
        lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_get_string_descriptor_ascii.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p, ctypes.c_int]
        lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_bulk_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_char_p, ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        ctx = ctypes.c_void_p()
        if lib.libusb_init(ctypes.byref(ctx)) != 0:
            return None
        _libusb, _ctx = lib, ctx
    return _libusb


def _usb_string(lib, handle, idx):
    if not idx:
        return ""
    buf = ctypes.create_string_buffer(256)
    n = lib.libusb_get_string_descriptor_ascii(handle, idx, buf, 256)
    return buf.raw[:n].decode("ascii", "replace") if n > 0 else ""


def _bulk_interfaces(lib, dev):
    """Yield (interface number, name index, ep_out, ep_in, max packet) of vendor-class bulk interfaces."""
    cfg = ctypes.POINTER(_ConfigDesc)()
    if lib.libusb_get_active_config_descriptor(dev, ctypes.byref(cfg)) != 0:
        return
    try:
        for i in range(cfg.contents.bNumInterfaces):
            itf = cfg.contents.interface[i]
            for a in range(itf.num_altsetting):
                d = itf.altsetting[a]
                if d.bInterfaceClass != 0xFF or d.bNumEndpoints < 2:
                    continue
                eps = [d.endpoint[e] for e in range(d.bNumEndpoints)]
                bulk = [e for e in eps if (e.bmAttributes & 3) == 2]
                outs = [e for e in bulk if not e.bEndpointAddress & 0x80]
                ins = [e for e in bulk if e.bEndpointAddress & 0x80]
                if outs and ins:
                    yield (d.bInterfaceNumber, d.iInterface, outs[0].bEndpointAddress,
                           ins[0].bEndpointAddress, outs[0].wMaxPacketSize)
    finally:
        lib.libusb_free_config_descriptor(cfg)


def _bulk_probes():
    lib = _usb()
    if lib is None:
        return []
    probes = []
    devs = ctypes.POINTER(ctypes.c_void_p)()
    n = lib.libusb_get_device_list(_ctx, ctypes.byref(devs))
    try:
        for i in range(max(n, 0)):
            dev = devs[i]
            dd = _DeviceDesc()
            if lib.libusb_get_device_descriptor(dev, ctypes.byref(dd)) != 0:
                continue
            itfs = list(_bulk_interfaces(lib, dev))
            if not itfs:
                continue
            h = ctypes.c_void_p()
            if lib.libusb_open(dev, ctypes.byref(h)) != 0:
                continue  # no permission or busy
            try:
                product = _usb_string(lib, h, dd.iProduct)
                serial = _usb_string(lib, h, dd.iSerialNumber)
                for num, iname, ep_out, ep_in, mps in itfs:
                    if "CMSIS-DAP" in _usb_string(lib, h, iname):
                        probes.append({"transport": "bulk", "vid": dd.idVendor, "pid": dd.idProduct,
                                       "interface": num, "ep_out": ep_out, "ep_in": ep_in, "mps": mps,
                                       "product": product, "serial": serial})
            finally:
                lib.libusb_close(h)
    finally:
        if n >= 0:
            lib.libusb_free_device_list(devs, 1)
    return probes


class BulkTransport:
    def __init__(self, info):
        lib = _usb()
        self.lib, self.info, self.handle = lib, info, None
        devs = ctypes.POINTER(ctypes.c_void_p)()
        n = lib.libusb_get_device_list(_ctx, ctypes.byref(devs))
        try:
            for i in range(max(n, 0)):
                dd = _DeviceDesc()
                lib.libusb_get_device_descriptor(devs[i], ctypes.byref(dd))
                if (dd.idVendor, dd.idProduct) != (info["vid"], info["pid"]):
                    continue
                h = ctypes.c_void_p()
                if lib.libusb_open(devs[i], ctypes.byref(h)) != 0:
                    continue
                if _usb_string(lib, h, dd.iSerialNumber) == info["serial"]:
                    self.handle = h
                    break
                lib.libusb_close(h)
        finally:
            if n >= 0:
                lib.libusb_free_device_list(devs, 1)
        if self.handle is None:
            raise DebugError("CMSIS-DAP v2 probe disappeared")
        lib.libusb_set_auto_detach_kernel_driver(self.handle, 1)
        if lib.libusb_claim_interface(self.handle, info["interface"]) != 0:
            lib.libusb_close(self.handle)
            raise DebugError("cannot claim CMSIS-DAP interface (in use by another program?)")
        self.report_size = None  # bulk packets are not padded
        # Bootstrap DAP_Info with one USB packet, then use its negotiated limit.
        self.packet_size = info["mps"]
        while True:  # drop stale responses
            try:
                self._read(0.05)
            except DebugError:
                break

    def _read(self, timeout):
        # A full USB packet is not a message terminator. Requesting more than
        # one DAP response can merge queued replies, or wait for another reply
        # until timeout (e.g. a 512-byte response in a 1024-byte USB read).
        buf = ctypes.create_string_buffer(self.packet_size)
        got = ctypes.c_int()
        r = self.lib.libusb_bulk_transfer(self.handle, self.info["ep_in"], buf, self.packet_size, ctypes.byref(got),
                                          int(timeout * 1000))
        if r != 0:
            raise DebugError("CMSIS-DAP bulk read failed (%d)" % r)
        return buf.raw[:got.value]

    def send(self, data):
        data = bytes(data)
        got = ctypes.c_int()
        r = self.lib.libusb_bulk_transfer(self.handle, self.info["ep_out"], data, len(data), ctypes.byref(got),
                                          int(TIMEOUT_S * 1000))
        if r != 0 or got.value != len(data):
            raise DebugError("CMSIS-DAP bulk write failed (%d)" % r)

    def recv(self):
        return self._read(TIMEOUT_S)

    def xfer(self, data):
        self.send(data)
        return self.recv()

    def close(self):
        self.lib.libusb_release_interface(self.handle, self.info["interface"])
        self.lib.libusb_close(self.handle)


# --------------------------------------------------------------------------- probe

def find_probes():
    """Bulk (v2) interfaces first; a v1 HID interface of the same probe is listed after it."""
    return _bulk_probes() + _hidraw_probes()


class CMSISDAP(DAP):
    name = "CMSIS-DAP"

    def __init__(self, speed=4000, serial=None):
        super().__init__()
        if not 1 <= speed <= 0xFFFFFFFF // 1000:
            raise DebugError("SWD speed must be a positive kHz value fitting 32-bit Hz")
        probes = [p for p in find_probes() if not serial or p["serial"] == serial]
        if not probes:
            raise DebugError("no CMSIS-DAP probe found" + (" with serial %s" % serial if serial else ""))
        info = probes[0]
        self.t = BulkTransport(info) if info["transport"] == "bulk" else HidTransport(info["path"])
        self.name = "CMSIS-DAP %s (%s)" % ("v2" if info["transport"] == "bulk" else "v1", info["product"])
        try:
            self.packet_size = struct.unpack("<H", self.info(INFO_PACKET_SIZE)[:2])[0]
            if self.t.report_size is not None:
                self.t.report_size = self.packet_size
            else:
                self.t.packet_size = self.packet_size
            # How many command packets the probe can buffer; keep that many in flight.
            self.packet_count = max(1, self.info(INFO_PACKET_COUNT)[0])
            caps = self.info(INFO_CAPABILITIES)[0]
            if not caps & 1:
                raise DebugError("probe does not support SWD")
            self.fw = self.info(INFO_FW_VERSION).rstrip(b"\x00").decode("ascii", "replace")
            self.speed = speed
            self._init_swd(speed)
        except Exception:
            self.t.close()
            raise

    # ---- raw commands ----
    def cmd(self, data):
        resp = self.t.xfer(data)
        if not resp or resp[0] != data[0]:
            raise DebugError("CMSIS-DAP command 0x%02x: bad response" % data[0])
        return resp

    def cmds(self, packets):
        """Send several commands with up to packet_count of them in flight; return all responses."""
        out = []
        sent = 0
        while len(out) < len(packets):
            while sent < len(packets) and sent - len(out) < self.packet_count:
                self.t.send(packets[sent])
                sent += 1
            resp = self.t.recv()
            want = packets[len(out)][0]
            if not resp or resp[0] != want:
                raise DebugError("CMSIS-DAP command 0x%02x: bad response" % want)
            out.append(resp)
        return out

    def _status(self, data):
        if self.cmd(data)[1] != 0:
            raise DebugError("CMSIS-DAP command 0x%02x failed" % data[0])

    def info(self, what):
        r = self.cmd(bytes([DAP_INFO, what]))
        return r[2:2 + r[1]]

    def _init_swd(self, speed_khz):
        if self.cmd(bytes([DAP_CONNECT, 1]))[1] != 1:
            raise DebugError("probe refused SWD mode")
        self._status(struct.pack("<BI", DAP_SWJ_CLOCK, speed_khz * 1000))
        # idle cycles 0, WAIT retries 100, match retries 0
        self._status(struct.pack("<BBHH", DAP_TRANSFER_CONFIGURE, 0, 100, 0))
        self._status(bytes([DAP_SWD_CONFIGURE, 0]))  # 1-cycle turnaround, no data phase on WAIT/FAULT
        self._swj(56, b"\xff" * 7)                    # line reset
        self._swj(16, b"\x9e\xe7")                    # JTAG-to-SWD
        self._swj(56, b"\xff" * 7)                    # line reset
        self._swj(8, b"\x00")                         # idle
        try:
            self.dpidr()
        except DebugError:
            raise DebugError("no SWD response (is the debug port enabled / wired to GPIO_13/14?)")
        self.clear_errors()
        self.cmd(bytes([DAP_HOST_STATUS, 0, 1]))     # "connected" LED on

    def reconnect(self):
        """Redo the SWD line reset and DP init (after the target reset or the link dropped)."""
        self.cur_ap = None
        self._init_swd(self.speed)

    def _swj(self, bits, data):
        self._status(bytes([DAP_SWJ_SEQUENCE, bits & 0xFF]) + data)

    @staticmethod
    def _req(reg, apndp, read):
        return (apndp & 1) | (2 if read else 0) | ((reg & 3) << 2)

    def _check(self, resp, what):
        ack = resp & 7
        if ack == ACK_OK and not resp & 0x18:
            return
        self.clear_errors()
        raise DebugError("%s: %s" % (what, {ACK_WAIT: "WAIT", ACK_FAULT: "FAULT"}.get(ack, "no ACK / protocol error")))

    # ---- DAP backend interface ----
    def rd(self, reg, apndp):
        r = self.cmd(bytes([DAP_TRANSFER, 0, 1, self._req(reg, apndp, True)]))
        if len(r) < 3:
            raise DebugError("truncated DAP_Transfer response")
        self._check(r[2], "read %s[%d]" % ("AP" if apndp else "DP", reg))
        if r[1] != 1 or len(r) < 7:
            raise DebugError("incomplete DAP_Transfer read")
        return struct.unpack_from("<I", r, 3)[0]

    def wr(self, reg, apndp, val):
        r = self.cmd(struct.pack("<BBBBI", DAP_TRANSFER, 0, 1, self._req(reg, apndp, False), val & 0xFFFFFFFF))
        if len(r) < 3:
            raise DebugError("truncated DAP_Transfer response")
        self._check(r[2], "write %s[%d]" % ("AP" if apndp else "DP", reg))
        if r[1] != 1:
            raise DebugError("incomplete DAP_Transfer write")

    def transfer(self, ops):
        """Pack the accesses into as few DAP_Transfer packets as possible and pipeline them."""
        packets, shape = [], []
        i = 0
        while i < len(ops):
            req = bytearray([DAP_TRANSFER, 0, 0])
            count = nreads = 0
            while i < len(ops) and count < 255:
                reg, apndp, val = ops[i]
                if val is None:
                    if len(req) + 1 > self.packet_size or 3 + 4 * (nreads + 1) > self.packet_size:
                        break
                    req.append(self._req(reg, apndp, True))
                    nreads += 1
                else:
                    if len(req) + 5 > self.packet_size:
                        break
                    req.append(self._req(reg, apndp, False))
                    req += struct.pack("<I", val & 0xFFFFFFFF)
                count += 1
                i += 1
            req[2] = count
            packets.append(bytes(req))
            shape.append((count, nreads))
        out = []
        for r, (count, nreads) in zip(self.cmds(packets), shape):
            if r[1] != count:
                self._check(r[2], "transfer %d/%d" % (r[1], count))
                raise DebugError("transfer stopped after %d of %d accesses" % (r[1], count))
            self._check(r[2], "transfer")
            out += struct.unpack_from("<%dI" % nreads, r, 3)
        return out

    def rd_repeat(self, reg, apndp, n):
        chunk = (self.packet_size - 4) // 4
        sizes = [min(chunk, n - k) for k in range(0, n, chunk)]
        req = self._req(reg, apndp, True)
        packets = [struct.pack("<BBHB", DAP_TRANSFER_BLOCK, 0, c, req) for c in sizes]
        out = []
        for r, cnt in zip(self.cmds(packets), sizes):
            done = struct.unpack_from("<H", r, 1)[0]
            self._check(r[3], "block read %s[%d]" % ("AP" if apndp else "DP", reg))
            if done != cnt:
                raise DebugError("block read stopped after %d of %d words" % (done, cnt))
            out += struct.unpack_from("<%dI" % cnt, r, 4)
        return out

    def wr_repeat(self, reg, apndp, values):
        chunk = (self.packet_size - 5) // 4
        req = self._req(reg, apndp, False)
        parts = [values[k:k + chunk] for k in range(0, len(values), chunk)]
        packets = [struct.pack("<BBHB", DAP_TRANSFER_BLOCK, 0, len(p), req) +
                   struct.pack("<%dI" % len(p), *(v & 0xFFFFFFFF for v in p)) for p in parts]
        for r, p in zip(self.cmds(packets), parts):
            self._check(r[3], "block write")
            if struct.unpack_from("<H", r, 1)[0] != len(p):
                raise DebugError("block write incomplete")

    def clear_errors(self):
        try:
            self.cmd(struct.pack("<BBI", DAP_WRITE_ABORT, 0, 0x1E))
        except DebugError:
            pass

    def close(self):
        try:
            self.cmd(bytes([DAP_HOST_STATUS, 0, 0]))
            self.cmd(bytes([DAP_DISCONNECT]))
        except DebugError:
            pass
        self.t.close()


if __name__ == "__main__":
    for p in find_probes():
        print("%-5s %-40s serial=%s %s" % (p["transport"], p["product"], p["serial"],
                                           p.get("path", "if%d" % p.get("interface", -1))))
