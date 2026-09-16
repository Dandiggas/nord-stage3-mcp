"""Nord Stage 3 MCP: inventory, backups, approved writes and gig preparation.

Uses the captured Zevs FileTransfer v10 protocol and standard USB-MIDI for
notes. Mutating tools require explicit approval. Delete/erase are not exposed.
Nord Sound Manager must release the vendor USB interface before use.
"""

import atexit
import hashlib
import os
import re
import struct
import sys
import threading
import time

import usb.core
from fastmcp import FastMCP

import zevs
from gig_prep import GigPrep
from song_library import SongLibrary
from song_patch import SongPatch
from dependencies import parse_dependencies, resolve_dependencies
from program_edit import inspect_program, prepare_transpose, inspect_layout, prepare_layout
from inventory import Nord, NONNATIVE, tag

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(PROJECT_DIR, "downloads")

PARTITION_NAMES = {
    1: "Piano", 3: "Piano Pedal", 5: "Samp Lib", 6: "SWave",
    7: "Program", 8: "Synth", 9: "Song", 10: "Live", 11: "Settings",
}

MIDIX = zevs.PROTO_MIDIX          # 13
MIDIX_VER = 0                     # from CMIDIXBase ctor (w2=0)
MIDIX_REQ_NOTE_ON = 3
MIDIX_ACK_NOTE_ON = 4
MIDIX_REQ_NOTE_OFF = 6
MIDIX_ACK_NOTE_OFF = 7

INSTRCTRL = zevs.PROTO_INSTRCTRL  # 10
INSTRCTRL_VER = 2                 # keyboard's max from QryProtocol; layouts are single-path
IC_SET_INSTR = 0
IC_QRY_GET_INSTR = 2
IC_RPY_GET_INSTR = 3
IC_QRY_GET_INSTR_PARAM = 6
IC_RPY_GET_INSTR_PARAM = 7

FT_REQ_FILE_OPEN = 12
FT_ACK_FILE_OPEN = 13
FT_REQ_FILE_CLOSE = 14
FT_ACK_FILE_CLOSE = 15
FT_REQ_FILE_READ = 18
FT_ACK_FILE_READ = 19
FT_REQ_FILE_COPY = 22
FT_REQ_FILE_MOVE = 24
FT_REQ_FILE_SWAP = 26
FT_REQ_FILE_RENAME = 28
FT_REQ_FILE_CREATE = 10
FT_REQ_FILE_WRITE = 16
READ_CHUNK = 4096  # proven stable across 57 consecutive live chunks

TYPE_TO_PARTITION = {"npno": 1, "npdl": 3, "nsmp": 5, "ns3f": 7,
                     "ns3y": 8, "ns3s": 9, "ns3l": 10, "ns3t": 11}

# FileTransfer strings are U32 length-prefixed ASCII without a terminator.
# Create: bank, slot, size, type, timestamp, category, name length, name.
# Write: bank, slot, offset, length, then raw bytes.
# Copy/move/swap: source bank/slot, destination bank/slot; partition belongs
# to the surrounding Begin/End session, not the operation payload.
# Mutations are never retried after an ambiguous USB response.

mcp = FastMCP(
    "nord-mcp",
    instructions=(
        "Control the connected Nord Stage 3 over USB. Read-only tools query the sound "
        "inventory; nord_audition_note plays a note on the keyboard itself. "
        "Writes require explicit approval. Prepare a saved setlist plan before applying it."
    ),
)

_lock = threading.RLock()
_state: dict[str, Nord | None] = {"nord": None}


def _connect() -> Nord:
    if _state["nord"] is None:
        _state["nord"] = Nord()
    return _state["nord"]


def _disconnect():
    if _state["nord"] is not None:
        try:
            _state["nord"].close()
        except Exception:
            pass
        _state["nord"] = None


atexit.register(_disconnect)


def _run(fn, *, retry=True):
    """Serialize USB traffic. Retry reads; never replay a mutating operation.

    The short backoffs matter on macOS: right after a handle releases the
    vendor interface, an immediate re-claim comes back EACCES and the first
    transfer can stall — the keyboard needs a beat between connections."""
    with _lock:
        delays = (0.0, 2.0, 4.0) if retry else (0.0,)
        last: Exception = RuntimeError("unreachable")
        for wait in delays:
            if wait:
                time.sleep(wait)
            try:
                return fn(_connect())
            except (usb.core.USBError, zevs.PacketError) as e:
                last = e
                _disconnect()
        raise last


def _check_partition(partition: int):
    if partition not in NONNATIVE:
        raise ValueError(
            f"partition {partition} is not a user partition; "
            f"valid: {NONNATIVE} ({', '.join(f'{p}={PARTITION_NAMES[p]}' for p in NONNATIVE)})"
        )


@mcp.tool
def nord_status() -> dict:
    """Check the Nord keyboard is connected and answering. Returns the device
    name and the raw Ctrl protocol/version replies (hex) as a comms check."""

    def op(n: Nord):
        prod = n.dev.product_name()
        _, _, pl_proto = n.dev.transact(zevs.PROTO_CTRL, 0, zevs.CTRL_QRY_PROTOCOL)
        _, _, pl_ver = n.dev.transact(zevs.PROTO_CTRL, 0, zevs.CTRL_QRY_VERSION)
        return {
            "connected": True,
            "device": prod,
            "ctrl_protocol_reply_hex": pl_proto.hex(),
            "ctrl_version_reply_hex": pl_ver.hex(),
            "note": "keyboard answering on the vendor bulk interface (intf 0)",
        }

    return _run(op)


@mcp.tool
def nord_list_partitions() -> list[dict]:
    """List the Nord's user-writable sound partitions (Piano, Program, Synth,
    Live, etc.) with their partition indexes — the IDs every other tool takes."""

    def op(n: Nord):
        out = []
        for p in NONNATIVE:
            ps = n.partstate(p)
            out.append({"partition": p, "name": PARTITION_NAMES[p],
                        "partstate_raw": list(ps)})
        return out

    return _run(op)


@mcp.tool
def nord_list_banks(partition: int) -> list[dict]:
    """List the banks inside one partition (bank index, name, slot capacity).
    Bank indexes are per-partition and are what nord_file_info takes."""

    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            banks = n.banklist(partition)
            return [{"partition": partition, "bank": g, "name": name, "capacity": cap}
                    for g, (name, cap) in enumerate(banks)]
        finally:
            n.end()

    return _run(op)


@mcp.tool
def nord_list_files(partition: int, bank: int | None = None) -> dict:
    """List sound files in a partition — every slot's name and 4-char type tag
    (npno=piano, nsmp=sample, ns3f=program, ns3y=synth, ns3s=song, ns3l=live).
    Optionally restrict to one bank index. Large partitions take a few seconds
    (each slot is a separate round-trip to the keyboard)."""

    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            banks = n.banklist(partition)
            wanted = [bank] if bank is not None else range(len(banks))
            files = []
            for g in wanted:
                if g < 0 or g >= len(banks):
                    raise ValueError(f"bank {g} out of range; partition {partition} has {len(banks)} banks")
                s, x, y = n.iterate(g, 0xFFFFFFFF)
                seen = set()
                while s == 0:
                    if x != g or y in seen or not 0 <= y < banks[g][1]:
                        raise RuntimeError("Invalid or repeated inventory slot")
                    seen.add(y)
                    f, name, _tail = n.fileinfo(x, y)
                    if f[0] != 0:
                        raise RuntimeError("Inventory changed while listing; retry")
                    files.append({"bank": g, "slot": y, "name": name, "type": tag(f[4])})
                    s, x, y = n.iterate(g, y)
                if s != 1:
                    raise RuntimeError(f"Inventory failed with status {s}")
            return {"partition": partition, "name": PARTITION_NAMES[partition],
                    "count": len(files), "files": files}
        finally:
            n.end()

    return _run(op)


@mcp.tool
def nord_file_info(partition: int, bank: int, slot: int) -> dict:
    """Get one file's details by bank+slot within a partition: name, type tag,
    size in bytes (f3), format version (f5), category (f7), and raw tail words."""

    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            f, name, tail = n.fileinfo(bank, slot)
            if f[0] != 0:
                return {"partition": partition, "bank": bank, "slot": slot,
                        "found": False,
                        "note": "keyboard reported no file at this slot"}
            return {"partition": partition, "bank": bank, "slot": slot,
                    "found": True, "name": name, "type": tag(f[4]),
                    "f3": f[3], "f5": f[5], "category": f[7], "tail_raw": list(tail)}
        finally:
            n.end()

    return _run(op)


@mcp.tool
def nord_audition_note(note: int = 60, velocity: int = 100,
                       duration_ms: int = 800, channel: int = 1) -> dict:
    """Play a note on the Nord itself — it sounds through its own outputs using
    whatever program is currently selected on the panel. Goes over standard
    USB-MIDI (the 'Nord Stage 3 MIDI Input' port), the same way any DAW would
    play it — proven audible. note/velocity are MIDI numbers (0-127), channel
    is 1-16 (default 1, the keyboard's factory global channel).

    Historical note: the vendor-protocol ReqNoteOn (Zevs MIDIX msg 3) gets
    clean acks but produces no sound with selector 0 or 2 — Nord Sound Manager
    never sends it either (no references in the binary). MIDI is the working
    path, so this tool uses it. See STATE.md."""

    if not 0 <= note <= 127:
        raise ValueError("note must be a MIDI note number 0-127")
    if not 1 <= velocity <= 127:
        raise ValueError("velocity must be 1-127")
    if not 10 <= duration_ms <= 5000:
        raise ValueError("duration_ms must be 10-5000")
    if not 1 <= channel <= 16:
        raise ValueError("channel must be 1-16")

    with _lock:
        out = _midi_out()
        try:
            on = 0x90 | (channel - 1)
            off = 0x80 | (channel - 1)
            out.send_message([on, note, velocity])
            time.sleep(duration_ms / 1000.0)
            out.send_message([off, note, 0])
        except Exception as e:
            global _midi
            _midi = None  # port may have re-enumerated; rebuild next call
            raise RuntimeError(f"MIDI send failed: {e}")
    return {"played": True, "note": note, "velocity": velocity,
            "duration_ms": duration_ms, "channel": channel, "via": "usb-midi"}


_midi = None


def _midi_out():
    """Shared MIDI output to the keyboard, resolved by port name each time."""
    global _midi
    import rtmidi
    if _midi is None:
        _midi = rtmidi.MidiOut()
    ports = _midi.get_ports()
    if _midi.is_port_open():
        names = [p for p in ports if "Nord" in p]
        if names:
            return _midi
        _midi.close_port()
    idx = next((i for i, p in enumerate(ports) if "Nord Stage" in p), None)
    if idx is None:
        raise RuntimeError(f"Nord MIDI port not found; ports present: {ports}")
    _midi.open_port(idx)
    return _midi


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()
    return s or "unnamed"


@mcp.tool
def nord_download_file(partition: int, bank: int, slot: int) -> dict:
    """Download one sound file off the keyboard onto this Mac (backup path —
    the reverse of Nord Sound Manager's drag-to-instrument). Saves under
    downloads/<Partition name>/ in the nord-mcp project folder, named after
    the sound with its real Nord extension (.npno, .nsmp, .ns3f, ...).
    The exact byte size comes from FileInfo f3 (proven: the 42 piano files
    sum to the piano partition's used flash within 5 128KiB blocks), and no
    read ever starts at or past the file's end — reading past the end crashes
    the keyboard's USB stack (firmware bug, hit once during development).
    Large pianos (200+ MB) take a while: one USB round-trip per 4 KiB."""

    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        opened = False
        try:
            f, name, _tail = n.fileinfo(bank, slot)
            if f[0] != 0:
                raise ValueError(f"no file at bank {bank} slot {slot}")
            size = f[3]
            ftype = tag(f[4])
            pl = n.t(FT_REQ_FILE_OPEN, struct.pack(">II", bank, slot))
            opened = True
            (status, _, _) = struct.unpack(">III", pl[:12])
            if status != 0:
                raise RuntimeError(f"FileOpen failed, status {status}")
            data = bytearray()
            while len(data) < size:
                want = min(READ_CHUNK, size - len(data))
                pl = n.t(FT_REQ_FILE_READ,
                         struct.pack(">IIII", bank, slot, len(data), want))
                a = struct.unpack(">5I", pl[:20])
                if a[0] != 0:
                    raise RuntimeError(
                        f"FileRead failed at offset {len(data)}, status {a[0]}")
                if a[1:4] != (bank, slot, len(data)) or not 0 < a[4] <= want or len(pl) != 20 + a[4]:
                    raise RuntimeError("FileRead reply has wrong address, offset or length")
                chunk = pl[20:20 + a[4]]
                if not chunk:
                    raise RuntimeError(
                        f"short read at offset {len(data)} (got 0 of {want})")
                data += chunk
            data = bytes(data[:size])
            outdir = os.path.join(DOWNLOADS_DIR, _safe_name(PARTITION_NAMES[partition]))
            os.makedirs(outdir, exist_ok=True)
            outpath = os.path.join(outdir, f"{bank:02d}-{slot:03d}-{_safe_name(name)}-{hashlib.sha256(data).hexdigest()}.{ftype}")
            with open(outpath, "wb") as fh:
                fh.write(data)
            return {"partition": partition, "bank": bank, "slot": slot,
                    "name": name, "type": ftype, "size": size, "category": f[7],
                    "downloaded": len(data), "ok": len(data) == size,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "path": outpath}
        finally:
            if opened:
                try:
                    n.t(FT_REQ_FILE_CLOSE, struct.pack(">II", bank, slot))
                except usb.core.USBError:
                    pass
            n.end()

    return _run(op)


@mcp.tool
def nord_get_instrument() -> dict:
    """Ask the keyboard which instrument engine is in focus (InstrCtrl
    GetInstr). NOTE: at protocol v2 (the only version the keyboard answers)
    the keyboard replies (0, 8, "Obsolete") — this query is a v0 leftover.
    Use nord_get_instr_param(0) for live panel state."""

    def op(n: Nord):
        _, m, pl = n.dev.transact(INSTRCTRL, INSTRCTRL_VER, IC_QRY_GET_INSTR)
        if m != IC_RPY_GET_INSTR:
            raise RuntimeError(f"unexpected reply to GetInstr: msg {m}")
        a = pl[0]
        (b,) = struct.unpack_from(">I", pl, 1)
        name = pl[5:].split(b"\x00", 1)[0].decode("ascii", "replace")
        return {"field_u8": a, "field_u32": b, "name": name,
                "raw_hex": pl.hex()}

    return _run(op)


@mcp.tool
def nord_get_instr_param(param: int = 0) -> dict:
    """Read one instrument parameter from the panel (InstrCtrl GetInstrParam,
    protocol version 2 — the only version the keyboard answers). Param 0
    (engine focus — which slot is selected) is the only known-answering id.
    DO NOT probe other ids: unanswered messages wedge the keyboard's vendor
    interface for tens of seconds (every later message times out too)."""

    if param != 0:
        raise ValueError("only param 0 is known-answering; other ids wedge the link")

    def op(n: Nord):
        _, m, pl = n.dev.transact(INSTRCTRL, INSTRCTRL_VER,
                                  IC_QRY_GET_INSTR_PARAM, bytes([param]))
        if m != IC_RPY_GET_INSTR_PARAM:
            raise RuntimeError(f"unexpected reply to GetInstrParam: msg {m}")
        echo = pl[0]
        (value,) = struct.unpack_from(">i", pl, 1)
        return {"param": echo, "value": value, "raw_hex": pl.hex()}

    return _run(op)


# ---------------------------------------------------------------------------
# WRITE TOOLS — approval-gated.
# Safety rule: pass confirm=True ONLY when the user explicitly asked for THIS exact
# operation in the current turn. No confirm, no write. Move/Copy refuse an
# occupied destination outright — overwrite is impossible through these tools.
# Delete/Erase are deliberately NOT exposed.
# ---------------------------------------------------------------------------

def _require_confirm(confirm: bool):
    if confirm is not True:
        raise PermissionError(
            "write refused: pass confirm=True only with the user's explicit "
            "approval for this exact operation in the current turn")


def _file_at(n: Nord, bank: int, slot: int):
    """(name, type) if a file occupies bank/slot, else None. Caller begun partition."""
    f, name, _ = n.fileinfo(bank, slot)
    return (name, tag(f[4])) if f[0] == 0 else None


def _write_op(partition: int, msg: int, fields: tuple, what: str) -> dict:
    def op(n: Nord):
        n.begin(partition)
        try:
            bank, slot, dst_bank, dst_slot = fields
            banks = n.banklist(partition)
            for b, s in ((bank, slot), (dst_bank, dst_slot)):
                if not 0 <= b < len(banks) or not 0 <= s < banks[b][1]:
                    raise ValueError('Bank or slot is outside this partition')
            if (bank, slot) == (dst_bank, dst_slot):
                raise ValueError('Source and destination must be different')
            before = _file_at(n, bank, slot)
            destination = _file_at(n, dst_bank, dst_slot)
            if before is None:
                raise ValueError('Source is empty')
            if msg == FT_REQ_FILE_SWAP:
                if destination is None:
                    raise ValueError('Both slots must be occupied for swap')
            elif destination is not None:
                raise ValueError('Destination is occupied')
            pl = n.t(msg, struct.pack(">4I", *fields))
            (status,) = struct.unpack(">I", pl[:4])
            if status != 0:
                raise RuntimeError(f"{what} failed, keyboard status {status}")
            n.partstate(partition)
            after_source = _file_at(n, bank, slot)
            after_destination = _file_at(n, dst_bank, dst_slot)
            if msg == FT_REQ_FILE_SWAP:
                verified = after_source == destination and after_destination == before
            elif msg == FT_REQ_FILE_MOVE:
                verified = after_source is None and after_destination == before
            else:
                # Copies can be auto-renamed by firmware. Gig prep checks bytes too.
                verified = after_source == before and after_destination is not None and after_destination[1] == before[1]
            if not verified:
                raise RuntimeError(f'{what} acknowledgement did not match re-read slots')
        finally:
            n.end()
        return {"ok": True, "verified": True, "partition": partition, what: fields}
    return _run(op, retry=False)


@mcp.tool
def nord_move_file(partition: int, bank: int, slot: int,
                   dst_bank: int, dst_slot: int, confirm: bool = False) -> dict:
    """Move a sound file to an EMPTY slot in the same partition (changes the
    keyboard's layout). APPROVAL-GATED: confirm=True only with the user's explicit
    per-action approval. Refuses if the destination is occupied (use
    nord_swap_files to exchange two occupied slots). Verifies source and
    destination state before writing."""

    _require_confirm(confirm)
    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            src = _file_at(n, bank, slot)
            if src is None:
                raise ValueError(f"no file at bank {bank} slot {slot}")
            if _file_at(n, dst_bank, dst_slot) is not None:
                raise ValueError(
                    f"destination bank {dst_bank} slot {dst_slot} is occupied; "
                    "use nord_swap_files instead")
        finally:
            n.end()
        return src

    src = _run(op)
    r = _write_op(partition, FT_REQ_FILE_MOVE,
                  (bank, slot, dst_bank, dst_slot), "moved")
    r["file"] = src[0]
    r["from"] = [bank, slot]
    r["to"] = [dst_bank, dst_slot]
    return r


@mcp.tool
def nord_swap_files(partition: int, bank_a: int, slot_a: int,
                    bank_b: int, slot_b: int, confirm: bool = False) -> dict:
    """Swap two occupied slots in the same partition (non-destructive —
    the two files exchange places). APPROVAL-GATED: confirm=True only with
    the user's explicit per-action approval. Verifies both slots before writing."""

    _require_confirm(confirm)
    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            a = _file_at(n, bank_a, slot_a)
            b = _file_at(n, bank_b, slot_b)
            if a is None or b is None:
                raise ValueError("both slots must be occupied to swap "
                                 f"(got {a} and {b})")
        finally:
            n.end()
        return a, b

    a, b = _run(op)
    r = _write_op(partition, FT_REQ_FILE_SWAP,
                  (bank_a, slot_a, bank_b, slot_b), "swapped")
    r["exchanged"] = {f"bank{bank_a}/slot{slot_a}": b[0],
                      f"bank{bank_b}/slot{slot_b}": a[0]}
    return r


@mcp.tool
def nord_copy_file(partition: int, bank: int, slot: int,
                   dst_bank: int, dst_slot: int, confirm: bool = False) -> dict:
    """Copy (duplicate) a sound file to an EMPTY slot in the same partition.
    APPROVAL-GATED: confirm=True only with the user's explicit per-action approval.
    Refuses if the destination is occupied."""

    _require_confirm(confirm)
    _check_partition(partition)

    def op(n: Nord):
        n.begin(partition)
        try:
            src = _file_at(n, bank, slot)
            if src is None:
                raise ValueError(f"no file at bank {bank} slot {slot}")
            if _file_at(n, dst_bank, dst_slot) is not None:
                raise ValueError(f"destination bank {dst_bank} slot {dst_slot} is occupied")
        finally:
            n.end()
        return src

    src = _run(op)
    r = _write_op(partition, FT_REQ_FILE_COPY,
                  (bank, slot, dst_bank, dst_slot), "copied")
    r["file"] = src[0]
    r["from"] = [bank, slot]
    r["to"] = [dst_bank, dst_slot]
    return r


MAX_NAME = 128          # writer caps strlen at 0x80 (CReqFileRename::Write)
TYPICAL_NAME = 16       # longest name observed in the live inventory is 15


def _pack_name(name: str) -> bytes:
    """Zevs string on the wire: U32 length + that many raw bytes, no terminator.
    Layout taken from the disassembly of CReqFileRename::Write / CReqFileCreate::Write."""
    b = name.encode("ascii")            # non-ASCII raises, which is what we want
    if not 1 <= len(b) <= MAX_NAME:
        raise ValueError(f"name must be 1..{MAX_NAME} ASCII characters, got {len(b)}")
    return struct.pack(">I", len(b)) + b


@mcp.tool
def nord_rename_file(partition: int, bank: int, slot: int, new_name: str,
                     confirm: bool = False) -> dict:
    """Rename the sound file in a slot. The file's contents are untouched, only
    its name changes. APPROVAL-GATED: confirm=True only with the user's explicit
    per-action approval. Refuses an empty slot, and verifies by re-reading the
    name from the keyboard afterwards."""

    _require_confirm(confirm)
    _check_partition(partition)
    name_field = _pack_name(new_name)

    def op(n: Nord):
        n.begin(partition)
        try:
            cur = _file_at(n, bank, slot)
            if cur is None:
                raise ValueError(f"no file at bank {bank} slot {slot}; nothing to rename")
            old_name = cur[0]

            pl = n.t(FT_REQ_FILE_RENAME,
                     struct.pack(">II", bank, slot) + name_field)
            (status,) = struct.unpack(">I", pl[:4])
            if status != 0:
                raise RuntimeError(f"rename failed, keyboard status {status}")

            n.partstate(partition)
            after = _file_at(n, bank, slot)
        finally:
            n.end()

        if after is None:
            raise RuntimeError("slot is empty after rename; investigate before writing again")
        return {
            "ok": True,
            "partition": partition,
            "bank": bank,
            "slot": slot,
            "old_name": old_name,
            "requested_name": new_name,
            "name_on_keyboard": after[0],
            "verified": after[0] == new_name,
            "type": after[1],
            "note": None if after[0] == new_name else
                    "keyboard stored a different name than requested (truncation or collision)",
        }

    r = _run(op, retry=False)
    if len(new_name) > TYPICAL_NAME:
        r["warning"] = (f"name is {len(new_name)} chars; longest in your inventory is 15, "
                        "the keyboard display may truncate it")
    return r


CBIN_MAGIC = b"CBIN"
CBIN_HEADER_LEN = 0x2C          # 44 bytes; content starts here
WRITE_CHUNK = 4096              # same size proven stable on the read path
PARTITION_TO_TYPE = {v: k for k, v in TYPE_TO_PARTITION.items()}


def _read_upload_source(path: str) -> dict:
    """Read a file for upload. Two shapes exist:

    - An NSM export, which starts with a versioned CBIN container and carries
      the metadata the keyboard needs (type tag, attr5, category).
    - A raw flash dump, which is what nord_download_file writes. It is the
      keyboard's own bytes with no container, so it carries NO attr5 and the
      caller has to supply one.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw:
        raise ValueError(f"{path} is empty")

    if raw[:4] == CBIN_MAGIC:
        if len(raw) < 8:
            raise ValueError(f"{path} has a truncated CBIN header")
        version = struct.unpack_from('<I', raw, 4)[0]
        if version not in (0, 1):
            raise ValueError(f"{path} has unsupported CBIN version {version}")
        # Legacy piano/sample exports use version 0, without the extra
        # twenty bytes added in version 1. Never discard payload as metadata.
        header_length = 24 if version == 0 else CBIN_HEADER_LEN
        if len(raw) <= header_length:
            raise ValueError(f"{path} has a CBIN header but no content")
        return {
            "kind": "cbin",
            "content": raw[header_length:],
            "type_tag": raw[8:12].decode("ascii", "replace"),
            "attr5": struct.unpack_from("<I", raw, 0x10)[0],
            "category": struct.unpack_from("<I", raw, 0x14)[0],
        }

    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return {
        "kind": "raw",
        "content": raw,
        "type_tag": ext if ext in TYPE_TO_PARTITION else None,
        "attr5": None,
        "category": None,
    }


@mcp.tool
def nord_upload_file(partition: int, bank: int, slot: int, path: str,
                     name: str | None = None, attr5: int | None = None,
                     confirm: bool = False) -> dict:
    """Upload a sound file from the computer into an EMPTY slot on the keyboard.

    APPROVAL-GATED: confirm=True only with the user's explicit per-action approval.
    Refuses an occupied destination, so this can never overwrite. Wire sequence
    is Begin(p) -> Create -> Write chunks -> Close -> End, verified by re-reading
    the slot afterwards.

    `path` may be an NSM export (24/44-byte CBIN container, carries its own
    metadata) or a raw flash dump from nord_download_file. A raw dump has no
    attr5, so pass one explicitly (observed value: 6 for programs).
    """

    _require_confirm(confirm)
    _check_partition(partition)

    src = _read_upload_source(path)
    content = src["content"]
    type_tag = src["type_tag"]
    if not type_tag or len(type_tag) != 4:
        raise ValueError(
            f"cannot determine the 4-character type tag for {path}; "
            f"expected one of {sorted(TYPE_TO_PARTITION)}")
    if TYPE_TO_PARTITION.get(type_tag) != partition:
        raise ValueError(
            f"{type_tag} files belong in partition {TYPE_TO_PARTITION.get(type_tag)}"
            f" ({PARTITION_NAMES.get(TYPE_TO_PARTITION.get(type_tag))}), not {partition}")

    eff_attr5 = attr5 if attr5 is not None else src["attr5"]
    if eff_attr5 is None:
        raise ValueError(
            f"{path} is a raw flash dump and carries no attr5; pass attr5 "
            "explicitly (6 was observed for programs)")

    up_name = name or os.path.splitext(os.path.basename(path))[0]
    name_field = _pack_name(up_name)
    tag_u32 = struct.unpack(">I", type_tag.encode("ascii"))[0]
    stamp = int(time.time())

    def op(n: Nord):
        n.begin(partition)
        try:
            if _file_at(n, bank, slot) is not None:
                raise ValueError(
                    f"bank {bank} slot {slot} is occupied; upload refuses to "
                    "overwrite, choose an empty slot")

            pl = n.t(FT_REQ_FILE_CREATE,
                     struct.pack(">6I", bank, slot, len(content), tag_u32,
                                 stamp, eff_attr5) + name_field)
            (status,) = struct.unpack(">I", pl[:4])
            if status != 0:
                raise RuntimeError(f"create failed, keyboard status {status}")

            written = 0
            try:
                while written < len(content):
                    chunk = content[written:written + WRITE_CHUNK]
                    pl = n.t(FT_REQ_FILE_WRITE,
                             struct.pack(">4I", bank, slot, written, len(chunk)) + chunk)
                    (status,) = struct.unpack(">I", pl[:4])
                    if status != 0:
                        raise RuntimeError(
                            f"write failed at offset {written}, keyboard status {status}")
                    written += len(chunk)
            finally:
                n.t(FT_REQ_FILE_CLOSE, struct.pack(">2I", bank, slot))

            n.partstate(partition)
            after = _file_at(n, bank, slot)
        finally:
            n.end()

        if after is None:
            raise RuntimeError(
                "slot is still empty after upload; the keyboard rejected it")
        downloaded = nord_download_file(partition, bank, slot)
        expected = hashlib.sha256(content).hexdigest()
        if downloaded['sha256'] != expected or downloaded['type'] != type_tag:
            raise RuntimeError('Uploaded content differs from source; investigate before writing again')
        return {
            "sha256": downloaded['sha256'],
            "readback_path": downloaded['path'],
            "ok": True,
            "partition": partition,
            "bank": bank,
            "slot": slot,
            "source": path,
            "source_kind": src["kind"],
            "type": type_tag,
            "bytes_sent": written,
            "chunks": (written + WRITE_CHUNK - 1) // WRITE_CHUNK,
            "attr5": eff_attr5,
            "requested_name": up_name,
            "name_on_keyboard": after[0],
            "verified": after[0] == up_name,
            "note": None if after[0] == up_name else
                    "keyboard stored a different name (truncation, or it "
                    "auto-renamed to avoid a collision)",
        }

    return _run(op, retry=False)


@mcp.tool
def nord_inspect_program(path: str) -> dict:
    """Read a local Stage 3 v3.04 .ns3f file's transpose setting. No USB writes."""
    from pathlib import Path
    if Path(path).suffix.lower() != '.ns3f':
        raise ValueError('Only .ns3f programs are supported')
    return inspect_program(Path(path).read_bytes())


@mcp.tool
def nord_prepare_transpose(path: str, semitones: int, enabled: bool = True) -> dict:
    """Save a backed-up edited program locally, with an exact before/after report.

    Supports only transpose (-6..+6 semitones) in Stage 3 v3.04 programs.
    Does not write to the keyboard. Review the report before an approved upload
    to an empty slot. The output preserves all other payload bits.
    """
    return prepare_transpose(path, semitones, enabled,
                             os.path.join(PROJECT_DIR, 'program-edits'))


@mcp.tool
def nord_inspect_layout(path: str) -> dict:
    """Inspect stored Stage 3 v3.04 panel, split, part level and controller settings.

    Stored zones are not a guarantee of audible routing. No hardware writes.
    """
    from pathlib import Path
    if Path(path).suffix.lower() != '.ns3f':
        raise ValueError('Only .ns3f programs are supported')
    return inspect_layout(Path(path).read_bytes())


@mcp.tool
def nord_prepare_layout(path: str, changes: dict) -> dict:
    """Back up and prepare an edited Stage 3 v3.04 program locally, without USB writes.

    changes supports panels (A/B/AB), split ({enabled:true,note:C4,width:1} or
    {enabled:false}), parts ({A.piano:{enabled:true,level:100,zone:left},
    A.synth:{enabled:true,level:70,zone:right,control_pedal_level:110}}).
    Part keys accept A/B piano/synth/organ. Level is 0..127; zone is left/right/full.
    wheel_level, aftertouch_level and control_pedal_level set full-controller
    target levels. For a silent-to-full wheel fade use level:0,wheel_level:127;
    silence requires other level controllers also at their starting positions.
    Synth parts also accept disable_wheel_filter:true to remove wheel control of
    filter cutoff while retaining its base setting and other controllers.
    Split notes: F2,C3,F3,C4,F4,C5,F5,C6,F6,C7. Width: 1,6,12 semitones.
    Left/right requires a single middle split. Unknown controls and Dual Keyboard
    programs are rejected. Uses existing sounds only; sample availability and
    audio are not verified. Review the returned report before an approved upload.
    """
    return prepare_layout(path, changes, os.path.join(PROJECT_DIR, 'program-edits'))


@mcp.tool
def nord_check_program_dependencies(bank: int, slot: int) -> dict:
    """Ask the Nord which piano/sample sounds a stored program needs and resolve them.

    Read-only. Uses the keyboard's own dependency identity resolution, then checks
    exact returned names/types against current installed inventory. Active missing
    or unsupported references block readiness; unused references remain warnings.
    Applies to stored Stage 3 programs, not unuploaded local drafts. Does not
    install, substitute or delete sounds. Does not prove audible performance.
    """
    def op(n):
        n.begin(7)
        try:
            banks = n.banklist(7)
            if not 0 <= bank < len(banks) or not 0 <= slot < banks[bank][1]:
                raise ValueError('Program address out of range')
            fields, name, tail = n.fileinfo(bank, slot)
            if fields[0] != 0 or tag(fields[4]) != 'ns3f':
                raise ValueError('A stored ns3f program is required')
            rows = parse_dependencies(n.t(40, struct.pack('>II', bank, slot)), bank, slot)
            if len(rows) != 4:
                raise RuntimeError('Unsupported Stage 3 program dependency layout')
        finally:
            n.end()
        inventories = {}
        for partition in sorted({r['partition'] for r in rows if r['kind'] == 0 and r['partition'] in (1, 5)}):
            inventories[partition] = nord_list_files(partition)['files']
        n.begin(7)
        try:
            later, later_name, later_tail = n.fileinfo(bank, slot)
            # The first tail word is volatile on this firmware. Last word is CRC.
            if tuple(later) != tuple(fields) or later_name != name or later_tail[-1] != tail[-1]:
                raise RuntimeError('Program changed during dependency check')
            repeated = parse_dependencies(n.t(40, struct.pack('>II', bank, slot)), bank, slot)
            if repeated != rows:
                raise RuntimeError('Dependencies changed during check')
        finally:
            n.end()
        report = resolve_dependencies(rows, inventories)
        report.update({'bank': bank, 'slot': slot, 'name': name,
                       'checked_by': 'Nord dependency query plus current inventory',
                       'hardware_written': False,
                       'note': 'Inactive unresolved references can become required when enabling another part.'})
        return report
    return _run(op)


GIG_PLANS_DIR = os.path.join(PROJECT_DIR, 'gig-plans')


def _song_library():
    return SongLibrary(sys.modules[__name__], os.path.join(os.path.dirname(GIG_PLANS_DIR), 'song-library.json'))


@mcp.tool
def nord_remember_song(title: str, patches: list[dict], artist: str = '',
                       version: str = 'default', key: str = '', notes: str = '',
                       source_reference: str = '', replace: bool = False) -> dict:
    """Remember a chosen sound or ordered patch sequence for a song, locally.

    Each patch has source_bank/source_slot (zero-based), optional label and notes.
    Reads and fingerprints the actual Programs; never writes to the keyboard.
    Store only deliberate choices, not guesses from approximate name matches.
    Artist/version distinguish arrangements. Key is a performance note, not a
    transposition command. Replacing an existing title/artist/version requires
    replace=True; no entry is silently overwritten. Returns the saved file path.
    """
    with _lock:
        return _song_library().remember(title, patches, artist, version, key, notes, source_reference, replace)


@mcp.tool
def nord_list_song_choices(query: str = '') -> list[dict]:
    """Find remembered songs and versions. Reads local records, not current hardware.

    Preparation checks remembered source names and bytes against the live Nord.
    """
    return _song_library().list(query)


@mcp.tool
def nord_prepare_setlist(gig: str, songs: list[dict], start_bank: int,
                         start_slot: int = 0, source_reference: str = '',
                         previous_plan_id: str | None = None) -> dict:
    """Prepare a gig without writing to the keyboard. Supply songs in running order.

    Each song needs title; optional program selects an exact program name, or
    source_bank/source_slot selects an explicit source (zero-based). Optional key
    records the musical key but does NOT transpose. Optional name sets the copied
    program's display name (1-15 ASCII characters). Repeated songs are supported.
    Agent reads the setlist via Inbox HQ, extracts songs/sections and preserves
    the email reference here. Ambiguous/missing matches return candidates and
    ready=False; resolve them before applying. Missing/unverified required sounds
    also block readiness and are checked again before any writes. Complete Programs preserve their
    existing sounds and splits. Synth presets alone cannot become full Programs.
    Destinations are consecutive EMPTY Program slots beginning at start_bank/slot.
    Ready plans include verified local source backups and an immutable plan_id.
    Present the exact rows to the user for approval before nord_apply_setlist.

    Songs can also have artist/version/notes/set, patches (ordered selectors with
    label/notes), or sections (ordered song objects inside a named medley).
    Without an explicit selector, remembered choices are reused only if their
    original source names and bytes still match. Ambiguous versions and changed
    sounds block application. Returns a printable local gig_sheet_path.
    For a changed running order, pass previous_plan_id to save a reviewed revision
    and prevent the old plan from being applied. New approval is required.
    """
    with _lock:
        return GigPrep(sys.modules[__name__], GIG_PLANS_DIR, _song_library()).prepare(
            gig, songs, start_bank, start_slot, source_reference, previous_plan_id)


@mcp.tool
def nord_get_setlist_plan(plan_id: str) -> dict:
    """Read a previously prepared plan by its plan_id, including exact slot changes."""
    return GigPrep(sys.modules[__name__], GIG_PLANS_DIR).load(plan_id)


@mcp.tool
def nord_apply_setlist(plan_id: str, confirm: bool = False) -> dict:
    """Apply the exact saved plan ONLY after the user approves its rows (confirm=True).

    Checks backups, source bytes and empty destinations again; copies and renames
    programs, then downloads every result to verify its name and bytes. Originals
    remain in place. A failed or interrupted attempt is journalled and cannot be
    replayed automatically. Repeating a completed plan only re-verifies it.
    """
    with _lock:
        return GigPrep(sys.modules[__name__], GIG_PLANS_DIR).apply(plan_id, confirm)


@mcp.tool
def nord_prepare_song_patch(title: str, brief: str, source_bank: int, source_slot: int,
                            bank: int, slot: int, name: str, changes: dict,
                            artist: str = '', version: str = 'default', key: str = '', notes: str = '',
                            samples: dict | None = None) -> dict:
    """Prepare a backed-up song-specific patch and readable preview without hardware writes.

    Agent turns the user's musical brief into explicit supported layout controls
    after choosing a suitable existing source. Required sounds are re-evaluated
    for newly enabled parts. Missing sounds, occupied destinations and existing
    remembered versions block readiness. Brief is recorded, not interpreted here.
    Uses nord_prepare_layout controls including reverb/compressor. Optional samples
    maps a destination part to {bank,slot,part} from a donor program. Selects only
    installed piano references or sample-mode synth references, retaining shaping.
    Sample installation and switching synth oscillator modes are unsupported.
    Apply only the reviewed plan. Application also remembers the new song/version.
    """
    with _lock:
        return SongPatch(sys.modules[__name__], os.path.join(PROJECT_DIR,'song-patch-plans'), _song_library()).prepare(
            title, brief, source_bank, source_slot, bank, slot, name, changes, artist, version, key, notes, samples)


@mcp.tool
def nord_get_song_patch_plan(plan_id: str) -> dict:
    """Read a saved song-patch plan, checking it has not changed."""
    return SongPatch(sys.modules[__name__], os.path.join(PROJECT_DIR,'song-patch-plans'), _song_library()).load(plan_id)


@mcp.tool
def nord_apply_song_patch(plan_id: str, confirm: bool = False) -> dict:
    """Apply an explicitly approved song-patch plan to an empty slot, verify and remember it.

    Rechecks source, edited file, required sounds and destination before writing.
    Records attempts before upload. Interrupted/failed attempts cannot be replayed.
    Completed plans only reverify on repeat. User must still audition the result.
    """
    with _lock:
        return SongPatch(sys.modules[__name__], os.path.join(PROJECT_DIR,'song-patch-plans'), _song_library()).apply(plan_id,confirm)


def _layout_restore():
    from layout_restore import LayoutRestore
    return LayoutRestore(sys.modules[__name__], os.path.join(PROJECT_DIR, 'layout-snapshots'))


@mcp.tool
def nord_snapshot_layout(name: str, banks: list[int]) -> dict:
    """Back up selected Program banks and their empty slots without keyboard writes.

    Does not snapshot samples, songs, live buffers or global instrument settings.
    """
    with _lock:
        return _layout_restore().snapshot(name, banks)


@mcp.tool
def nord_prepare_restore(snapshot_id: str, parking_banks: list[int]) -> dict:
    """Preview restoring saved bank positions, contents and original categories.

    Uses moves/swaps or uploads missing originals from verified snapshot backups.
    Parks edited/displaced programs outside restored banks; never erases them.
    Old backups lacking category metadata cannot be uploaded. Insufficient parking
    blocks readiness. Sample memory is outside this restoration's scope.
    """
    with _lock:
        return _layout_restore().prepare(snapshot_id, parking_banks)


@mcp.tool
def nord_prepare_program_space(start_bank: int, start_slot: int, count: int,
                               parking_banks: list[int]) -> dict:
    """Prepare a backed-up move plan to free a continuous gig range without erasing.

    Parking banks must be outside the gig range. Review all source/destination
    rows before applying via nord_apply_restore. References to moved slots become
    stale and must be deliberately refreshed; this tool never rewrites them.
    """
    with _lock:
        return _layout_restore().prepare_space(start_bank, start_slot, count, parking_banks)


@mcp.tool
def nord_apply_restore(restore_id: str, confirm: bool = False) -> dict:
    """Apply an explicitly reviewed restore plan, verify all bytes and bank positions.

    Failed/interrupted writes require inspection and cannot be replayed automatically.
    """
    with _lock:
        return _layout_restore().apply(restore_id, confirm)


if __name__ == "__main__":
    mcp.run()  # stdio transport
