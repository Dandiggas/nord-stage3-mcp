"""Full read-only sound inventory - the correct session recipe.

Program inventory session format:
  ReqBegin's U32 payload = PARTITION INDEX (not a version!). NSM runs one
  phase per non-native partition: Begin(p) -> PartState(p) -> walk banks
  0..N-1 -> ReqEnd. Walk: Iterate(g,-1,0) starts bank g; Iterate(g,idx,0)
  advances; s=1 = bank exhausted. FileInfo(g,idx) per entry.
  BankList(p) gives bank count + slot capacity per partition.

Non-native partitions: 1 Piano, 3 Piano Pedal, 5 Samp Lib, 6 SWave,
7 Program, 8 Synth, 9 Song, 10 Live, 11 Settings.
"""
import struct
import usb.core
import zevs

FT = zevs.PROTO_FILETRANSFER
VER = 10
FT_REQ_END = 6
NONNATIVE = [1, 3, 5, 6, 7, 8, 9, 10, 11]


def tag(v):
    b = struct.pack(">I", v)
    return b.decode("ascii") if all(32 <= c < 127 for c in b) else f"{v:#x}"


class Nord:
    def __init__(self):
        # First contact after a previous host session that ended dirty (process
        # died, claim raced) times out on the device side; a fresh claim plus a
        # short wait clears it. Retry the whole connect before giving up.
        import time
        last: Exception = usb.core.USBError("connect failed")
        for _ in range(3):
            try:
                self.dev = zevs.NordDevice.find()
                self.dev.transact(zevs.PROTO_CTRL, 0, zevs.CTRL_QRY_PROTOCOL, timeout_ms=4000)
                self.dev.transact(zevs.PROTO_CTRL, 0, zevs.CTRL_QRY_VERSION, timeout_ms=4000)
                return
            except usb.core.USBError as e:
                # macOS needs a real pause after release before the same
                # interface can be claimed again (re-claim too fast = EACCES).
                last = e
                try:
                    self.dev.close()
                except Exception:
                    pass
                time.sleep(2.5)
        raise last

    def t(self, msg, payload=b"", timeout=10000):
        p, m, pl = self.dev.transact(FT, VER, msg, payload, timeout_ms=timeout)
        return pl

    def begin(self, part):
        return self.t(zevs.FT_REQ_BEGIN, struct.pack(">I", part), timeout=60000)

    def end(self):
        try:
            return self.t(FT_REQ_END)
        except usb.core.USBTimeoutError:
            return b""

    def partstate(self, p):
        pl = self.t(zevs.FT_QRY_PART_STATE, struct.pack(">I", p))
        return struct.unpack(f">{len(pl)//4}I", pl[:len(pl)//4 * 4])

    def banklist(self, p):
        pl = self.t(zevs.FT_QRY_BANK_LIST, struct.pack(">I", p))
        count = pl[8]
        off = 9
        banks = []
        for _ in range(count):
            (slen,) = struct.unpack_from(">I", pl, off)
            off += 4
            name = pl[off:off + slen].decode("ascii", "replace")
            off += slen
            (cap,) = struct.unpack_from(">I", pl, off)
            off += 4
            banks.append((name, cap))
        return banks

    def iterate(self, a, b):
        pl = self.t(zevs.FT_QRY_FILE_ITERATE, struct.pack(">III", a, b, 0))
        return struct.unpack(">III", pl[:12])

    def fileinfo(self, g, idx):
        pl = self.t(zevs.FT_QRY_FILE_INFO, struct.pack(">II", g, idx))
        f = struct.unpack_from(">IIIIIIII", pl, 0)
        (slen,) = struct.unpack_from(">I", pl, 32)
        slen = min(slen, 127)
        name = pl[36:36 + slen].decode("ascii", "replace")
        tail = struct.unpack_from(">III", pl, 36 + slen) if len(pl) >= 48 + slen else (0, 0, 0)
        return f, name, tail

    def close(self):
        self.dev.close()
