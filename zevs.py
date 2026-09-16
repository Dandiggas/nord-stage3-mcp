"""Zevs protocol client for Nord keyboards.

Reverse engineered from Nord Sound Manager v9.16 (com.clavia.NordSoundManager)
by static analysis of the arm64 binary. Wire format (USB bulk, BIG-ENDIAN —
Zevs::CBitStreamO::Wr_U32/Wr_U16 emit MSB-first, verified in disasm at
0x100086650 / 0x100086558):

    [0]  U32 total_length   (includes this field and the trailing CRC)
    [4]  U32 protocol_id    (UI=6, Ctrl=7, InstrCtrl=10, FileTransfer=12, MIDIX=13)
    [8]  U32 protocol_version (Ctrl=0, FileTransfer=negotiated)
    [12] U32 message_id
    [16] payload ...
    [-2] U16 CRC16 over all preceding bytes (init 0xFFFF, MSB-first table,
         poly 0x1021 = CCITT-FALSE; table built by Zevs::CRC::CInitCRC @
         0x100086f80, poly constant 0x1021 visible at 0x100086fb0)

Message IDs are documented per protocol below, extracted from each message
class's Write()/Read() in the binary.

Session flow (Ymer::ProtocolManager::CProbe::OnUSBPortOpened @ 0x1000580a0):
first contact is Ctrl QryProtocol (msg 2), NOT QryVersion.
"""
import struct
import usb.core
import usb.util

VID_CLAVIA = 0x0FFC

PROTO_UI = 6
PROTO_CTRL = 7
PROTO_INSTRCTRL = 10
PROTO_FILETRANSFER = 12
PROTO_MIDIX = 13

# Ctrl message ids
CTRL_QRY_VERSION = 0
CTRL_RPY_VERSION = 1
CTRL_QRY_PROTOCOL = 2
CTRL_RPY_PROTOCOL = 3
CTRL_REQ_REBOOT = 4
CTRL_REQ_BOOT = 5
CTRL_REQ_SET_SERIAL = 8
CTRL_ACK_SET_SERIAL = 9

# FileTransfer message ids (subset)
FT_QRY_PART_LIST = 0
FT_RPY_PART_LIST = 1
FT_QRY_BANK_LIST = 2
FT_RPY_BANK_LIST = 3
FT_REQ_BEGIN = 4
FT_ACK_BEGIN = 5
FT_REQ_END = 6
FT_ACK_END = 7
FT_QRY_PART_STATE = 8
FT_RPY_PART_STATE = 9
FT_REQ_FILE_CREATE = 10
FT_ACK_FILE_CREATE = 11
FT_REQ_FILE_OPEN = 12
FT_ACK_FILE_OPEN = 13
FT_REQ_FILE_CLOSE = 14
FT_ACK_FILE_CLOSE = 15
FT_REQ_FILE_WRITE = 16
FT_ACK_FILE_WRITE = 17
FT_REQ_FILE_READ = 18
FT_ACK_FILE_READ = 19
FT_REQ_FILE_DELETE = 20
FT_ACK_FILE_DELETE = 21
FT_REQ_FILE_COPY = 22
FT_ACK_FILE_COPY = 23
FT_REQ_FILE_MOVE = 24
FT_ACK_FILE_MOVE = 25
FT_REQ_FILE_SWAP = 26
FT_ACK_FILE_SWAP = 27
FT_REQ_FILE_RENAME = 28
FT_ACK_FILE_RENAME = 29
FT_QRY_FILE_INFO = 30
FT_RPY_FILE_INFO = 31
FT_QRY_FILE_ITERATE = 32
FT_RPY_FILE_ITERATE = 33
FT_REQ_ERASE_BLOCK = 34
FT_REQ_ERASE_ALL = 36
FT_QRY_ERASE_STATUS = 38
FT_QRY_FILE_GET_DEPENDENCY = 40
FT_INVALIDATE_PART = 42
FT_INVALIDATE_BANK = 43
FT_INVALIDATE_FILE = 44
FT_REQ_INVALIDATE_ENABLE = 45
FT_REQ_FILE_SET_FOCUS = 47
FT_QRY_FILE_GET_FOCUS = 49
FT_REQ_FILE_SET_CATEGORY = 51
FT_REQ_FILE_SET_DEPENDENCY = 53
FT_REQ_ERASE_CANCEL = 55
FT_REQ_RESET = 57
FT_REQ_FILE_CONVERT = 59
FT_QRY_CONTENT_VERSION = 61


def crc16_ccitt(data: bytes, init: int = 0xFFFF, poly: int = 0x1021) -> int:
    """MSB-first CRC16 as implemented by Zevs::CRC::CCRC16 (init 0xFFFF)."""
    crc = init
    for b in data:
        crc = ((crc << 8) ^ _crc16_table_msb((b ^ (crc >> 8)) & 0xFF, poly)) & 0xFFFF
    return crc


_tab_cache = {}
def _crc16_table_msb(idx: int, poly: int) -> int:
    tab = _tab_cache.get(poly)
    if tab is None:
        tab = []
        for i in range(256):
            c = i << 8
            for _ in range(8):
                c = ((c << 1) ^ poly) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
            tab.append(c)
        _tab_cache[poly] = tab
    return tab[idx]


def build_packet(proto: int, proto_ver: int, msg_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack(">III", proto, proto_ver, msg_id) + payload
    length = 4 + len(body) + 2
    pkt = struct.pack(">I", length) + body
    return pkt + struct.pack(">H", crc16_ccitt(pkt))


class PacketError(Exception):
    pass


def parse_packet(buf: bytes) -> tuple[int, int, int, bytes]:
    if len(buf) < 18:
        raise PacketError(f"short packet: {len(buf)} bytes")
    (length,) = struct.unpack_from(">I", buf, 0)
    if length != len(buf):
        raise PacketError(f"length field {length} != buffer {len(buf)}")
    (crc_stored,) = struct.unpack_from(">H", buf, len(buf) - 2)
    crc_calc = crc16_ccitt(buf[:-2])
    if crc_stored != crc_calc:
        raise PacketError(f"CRC mismatch: got {crc_stored:04x}, calc {crc_calc:04x}")
    proto, ver, msg = struct.unpack_from(">III", buf, 4)
    return proto, ver, msg, buf[16:-2]


class NordDevice:
    """Raw USB transport to a Nord keyboard's vendor bulk interface."""

    def __init__(self, dev: usb.core.Device, intf, ep_out, ep_in, ep_int=None):
        self.dev = dev
        self.intf = intf
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.ep_int = ep_int

    @classmethod
    def find(cls, pid: int | None = None):
        for dev in usb.core.find(find_all=True, idVendor=VID_CLAVIA):
            if pid is not None and dev.idProduct != pid:
                continue
            cfg = dev.get_active_configuration()
            chosen = None
            for intf in cfg:
                eps = list(intf)
                bulk_out = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
                bulk_in = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                           and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
                intr_in = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                           and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR]
                if bulk_out and bulk_in:
                    chosen = (intf, bulk_out[0], bulk_in[0], intr_in[0] if intr_in else None)
                    break
            if chosen is None:
                continue
            intf, ep_out, ep_in, ep_int = chosen
            try:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
            return cls(dev, intf, ep_out, ep_in, ep_int)
        raise FileNotFoundError("no Clavia USB device with a bulk interface found")

    def transact(self, proto: int, proto_ver: int, msg_id: int,
                 payload: bytes = b"", timeout_ms: int = 5000) -> tuple[int, int, bytes]:
        pkt = build_packet(proto, proto_ver, msg_id, payload)
        self.ep_out.write(pkt, timeout=timeout_ms)
        # read one whole transfer (libusb discards what doesn't fit the buffer,
        # so never read a short length word first)
        buf = bytes(self.ep_in.read(65536, timeout=timeout_ms))
        p, v, m, pl = parse_packet(buf)
        return p, m, pl

    def product_name(self) -> str:
        return self.dev.product or "?"

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
        except Exception:
            pass
        usb.util.dispose_resources(self.dev)
