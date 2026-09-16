"""Bounded Stage 3 edits. Byte layout: https://chris55.github.io/nord-documentation/.

Only v3.04 ns3f payloads are accepted. No GPL viewer implementation is included.
"""
import hashlib
import json
from pathlib import Path
import struct
import zlib


def unpack_program(data: bytes) -> tuple[bytes, bytes]:
    header = b''
    if data.startswith(b'CBIN'):
        if len(data) != 592 or struct.unpack_from('<I', data, 4)[0] != 1:
            raise ValueError('Only 592-byte format-1 CBIN programs are supported')
        if data[8:12] != b'ns3f':
            raise ValueError('Only ns3f programs are supported')
        header, data = data[:44], data[44:]
        if struct.unpack_from('<I', header, 20)[0] != 304:
            raise ValueError('Only program version 3.04 is supported')
        if struct.unpack_from('<I', header, 24)[0] != zlib.crc32(data):
            raise ValueError('Program checksum mismatch')
    if len(data) != 548 or data[:5] != bytes.fromhex('0000013011'):
        raise ValueError('Only 548-byte program version 3.04 payloads are supported')
    if ((data[12] >> 3) & 15) > 12:
        raise ValueError('Invalid transpose encoding')
    return header, data


def inspect_program(data: bytes) -> dict:
    header, raw = unpack_program(data)
    value = ((raw[12] >> 3) & 15) - 6
    enabled = bool(raw[12] & 128)
    return {'version': '3.04', 'container': 'cbin' if header else 'raw',
            'payload_sha256': hashlib.sha256(raw).hexdigest(),
            'transpose': {'enabled': enabled, 'semitones': value,
                          'effective_semitones': value if enabled else 0}}


def transpose_program(data: bytes, semitones: int, enabled: bool = True) -> bytes:
    if type(semitones) is not int or not -6 <= semitones <= 6:
        raise ValueError('semitones must be an integer from -6 to +6')
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    header, raw = unpack_program(data)
    edited = bytearray(raw)
    edited[12] = (raw[12] & 7) | ((semitones + 6) << 3) | (128 if enabled else 0)
    if header:
        header = bytearray(header)
        struct.pack_into('<I', header, 24, zlib.crc32(edited))
    return bytes(header) + bytes(edited)


def prepare_transpose(path: str, semitones: int, enabled: bool, directory: str) -> dict:
    source = Path(path).resolve()
    if source.suffix.lower() != '.ns3f':
        raise ValueError('Only .ns3f files can be edited')
    before = source.read_bytes()
    after = transpose_program(before, semitones, enabled)
    digest = hashlib.sha256(after).hexdigest()
    folder = Path(directory) / digest
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / ('source-' + hashlib.sha256(before).hexdigest() + '.ns3f')
    output = folder / 'transposed.ns3f'
    for target, data in ((backup,before),(output,after)):
        if target.exists() and target.read_bytes() != data:
            raise RuntimeError('Saved edit artifact has changed; refusing to overwrite it')
        target.write_bytes(data)
    old_raw, new_raw = unpack_program(before)[1], unpack_program(after)[1]
    report = {'source_path': str(source), 'backup_path': str(backup), 'path': str(output),
              'file_sha256': digest, 'before': inspect_program(before),
              'after': inspect_program(after),
              'payload_changes': [{'offset': i, 'before': a, 'after': b}
                                  for i,(a,b) in enumerate(zip(old_raw,new_raw)) if a != b],
              'hardware_written': False,
              'note': 'Transpose only. Audio and hardware loading are not verified by preparation.'}
    report_path = folder / ('report-' + hashlib.sha256(before).hexdigest() + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2)+'\n')
    return report

# Physical parameter locations in the v3.04 payload, excluding the CBIN header.
PART_OFFSETS = {'piano': 0x43 - 44, 'synth': 0x52 - 44, 'organ': 0xb6 - 44}
SPLIT_NOTES = ('F2', 'C3', 'F3', 'C4', 'F4', 'C5', 'F5', 'C6', 'F6', 'C7')
ZONES = ('O---', '-O--', '--O-', '---O', 'OO--', '-OO-', '--OO', 'OOO-', '-OOO', 'OOOO')
HAND_ZONES = {'left': 4, 'right': 6, 'full': 9}
REVERB_TYPES = ('Room 1', 'Room 2', 'Stage 1', 'Stage 2', 'Hall 1', 'Hall 2')


def inspect_effects(raw):
    effects = {}
    for panel, shift in (('A', 0), ('B', 263)):
        rev = 0x134 - 44 + shift
        comp = 0x139 - 44 + shift
        code = (_word(raw, rev) >> 6) & 7
        amount = (_word(raw, rev + 1) >> 6) & 127
        morphs = {}
        for name, delta in (('wheel', 2), ('aftertouch', 3), ('control_pedal', 4)):
            encoded = (_word(raw, rev + delta) >> 6) & 255
            change = encoded - 127
            morphs[name] = {'enabled': change != 0, 'target_amount': max(0, min(127, amount + change))}
        effects[panel] = {
            'reverb': {'enabled': bool(raw[rev] & 2),
                       'type': REVERB_TYPES[code] if code < 6 else 'unsupported',
                       'bright': bool(raw[rev + 1] & 32), 'amount': amount,
                       'amount_controllers': morphs},
            'compressor': {'enabled': bool(raw[comp] & 32),
                           'amount': (_word(raw, comp) >> 6) & 127,
                           'fast': bool(raw[comp + 1] & 32)}}
    return effects


def edit_effects(raw, effects):
    if not isinstance(effects, dict) or not effects or set(effects) - {'A', 'B'}:
        raise ValueError('effects must map A or B to reverb/compressor controls')
    for panel, sections in effects.items():
        if not isinstance(sections, dict) or not sections or set(sections) - {'reverb', 'compressor'}:
            raise ValueError('Supported effects: reverb, compressor')
        shift = 263 if panel == 'B' else 0
        for kind, controls in sections.items():
            allowed = {'enabled', 'amount', 'type', 'bright', 'wheel_amount', 'aftertouch_amount', 'control_pedal_amount'} if kind == 'reverb' else {'enabled', 'amount', 'fast'}
            if not isinstance(controls, dict) or not controls or set(controls) - allowed:
                raise ValueError(f'Unsupported {kind} controls')
            offset = (0x134 if kind == 'reverb' else 0x139) - 44 + shift
            for key, delta, mask in (('enabled', 0, 2 if kind == 'reverb' else 32), ('bright', 1, 32), ('fast', 1, 32)):
                if key in controls:
                    if type(controls[key]) is not bool:
                        raise ValueError(f'{kind}.{key} must be a boolean')
                    raw[offset + delta] = (raw[offset + delta] & ~mask) | (mask if controls[key] else 0)
            if 'type' in controls:
                if controls['type'] not in REVERB_TYPES:
                    raise ValueError(f'Reverb type must be one of {REVERB_TYPES}')
                _put(raw, offset, 0x01c0, REVERB_TYPES.index(controls['type']) << 6)
            amount_offset = offset + (1 if kind == 'reverb' else 0)
            if 'amount' in controls:
                _put(raw, amount_offset, 0x1fc0, _level(controls['amount']) << 6)
            if kind == 'reverb':
                amount = (_word(raw, amount_offset) >> 6) & 127
                for key, delta in (('wheel_amount', 2), ('aftertouch_amount', 3), ('control_pedal_amount', 4)):
                    if key in controls:
                        _put(raw, offset + delta, 0x3fc0, (_level(controls[key]) - amount + 127) << 6)


def _word(raw, offset):
    return int.from_bytes(raw[offset:offset + 2], 'big')


def _put(raw, offset, mask, value):
    word = _word(raw, offset)
    raw[offset:offset + 2] = ((word & ~mask) | value).to_bytes(2, 'big')


def _level(value):
    if type(value) is not int or not 0 <= value <= 127:
        raise ValueError('level must be an integer from 0 to 127')
    return value


def inspect_layout(data: bytes) -> dict:
    _, raw = unpack_program(data)
    parts = {}
    for panel, shift in (('A', 0), ('B', 263)):
        for engine, base in PART_OFFSETS.items():
            offset = base + shift
            word = _word(raw, offset)
            level = (word >> 4) & 127
            zone = (word >> 11) & 15
            morphs = {}
            for name, delta in (('wheel', 1), ('aftertouch', 2), ('control_pedal', 3)):
                code = (_word(raw, offset + delta) >> 4) & 255
                change = (code & 127) + 1 if code & 128 else code - 127
                morphs[name] = {'enabled': change != 0,
                               'target_level': max(0, min(127, level + change))}
            parts[panel + '.' + engine] = {
                'enabled': bool(word & 0x8000), 'level': level,
                'stored_zone': ZONES[zone] if zone < len(ZONES) else 'unsupported',
                'level_controllers': morphs,
            }
            if engine == 'synth':
                parts[panel+'.'+engine]['wheel_filter_enabled'] = ((_word(raw,0x99-44+shift)>>3)&255) != 127
    points = {}
    for label, bit, code, width in (
        ('low', 8, (_word(raw, 5) >> 5) & 15, (raw[7] >> 3) & 3),
        ('mid', 4, (_word(raw, 5) >> 1) & 15, (raw[7] >> 1) & 3),
        ('high', 2, (_word(raw, 6) >> 5) & 15, (_word(raw, 7) >> 7) & 3),
    ):
        points[label] = {'enabled': bool(raw[5] & bit),
                         'note': SPLIT_NOTES[code] if code < 10 else 'unsupported',
                         'width': (1, 6, 12)[width] if width < 3 else 'unsupported'}
    mode = (raw[5] >> 5) & 3
    return {'panels': ('A', 'B', 'AB', 'unsupported')[mode],
            'dual_keyboard': bool(raw[14] & 8),
            'split': {'enabled': bool(raw[5] & 16), **points}, 'parts': parts,
            'effects': inspect_effects(raw)}


def edit_layout(data: bytes, changes: dict) -> bytes:
    """Edit explicit controls only; never substitute samples or infer a song sound."""
    header, source = unpack_program(data)
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Provide explicit layout changes')
    unknown = set(changes) - {'panels', 'split', 'parts', 'effects'}
    if unknown:
        raise ValueError(f'Unsupported layout controls: {sorted(unknown)}')
    if source[14] & 8:
        raise ValueError('Dual Keyboard programs are not supported for layout edits')
    raw = bytearray(source)
    if 'effects' in changes:
        edit_effects(raw, changes['effects'])
    if 'panels' in changes:
        if changes['panels'] not in ('A', 'B', 'AB'):
            raise ValueError('panels must be A, B or AB')
        raw[5] = (raw[5] & ~0x60) | (('A', 'B', 'AB').index(changes['panels']) << 5)
    split = changes.get('split')
    if split is not None:
        if not isinstance(split, dict) or set(split) - {'enabled', 'note', 'width'}:
            raise ValueError('Supported split controls: enabled, note, width')
        if type(split.get('enabled')) is not bool:
            raise ValueError('split.enabled must be a boolean')
        if split['enabled']:
            if split.get('note') not in SPLIT_NOTES:
                raise ValueError(f'Split note must be one of {SPLIT_NOTES}')
            width = split.get('width', 1)
            if type(width) is not int or width not in (1, 6, 12):
                raise ValueError('Split width must be 1, 6 or 12 semitones')
            raw[5] = (raw[5] & ~0x1e) | 0x14  # single middle split
            _put(raw, 5, 0x001e, SPLIT_NOTES.index(split['note']) << 1)
            raw[7] = (raw[7] & ~6) | ((1, 6, 12).index(width) << 1)
        else:
            if set(split) != {'enabled'}:
                raise ValueError('Disabled split takes only enabled=false')
            raw[5] &= ~0x10
    parts = changes.get('parts', {})
    if not isinstance(parts, dict):
        raise ValueError('parts must map A.piano, A.synth, A.organ or B equivalents to controls')
    simple_split = bool(raw[5] & 16) and (raw[5] & 14) == 4
    for name, controls in parts.items():
        if name not in inspect_layout(data)['parts']:
            raise ValueError(f'Unsupported part: {name}')
        if not isinstance(controls, dict) or not controls or set(controls) - {'enabled', 'level', 'zone', 'wheel_level', 'aftertouch_level', 'control_pedal_level', 'disable_wheel_filter'}:
            raise ValueError('Supported part controls: enabled, level, zone, wheel_level, aftertouch_level, control_pedal_level, disable_wheel_filter')
        panel, engine = name.split('.')
        offset = PART_OFFSETS[engine] + (263 if panel == 'B' else 0)
        if 'disable_wheel_filter' in controls:
            if engine != 'synth' or controls['disable_wheel_filter'] is not True:
                raise ValueError('disable_wheel_filter accepts only true on a synth part')
            # v3.04 cutoff-wheel offset: file 0x99 bits2..0 + 0x9a bits7..3.
            # Encoded 127 means no change; preserve cutoff, aftertouch and pedal.
            _put(raw,0x99-44+(263 if panel=='B' else 0),0x07f8,127<<3)
        if 'enabled' in controls:
            if type(controls['enabled']) is not bool:
                raise ValueError('Part enabled must be a boolean')
            _put(raw, offset, 0x8000, 0x8000 if controls['enabled'] else 0)
        if 'level' in controls:
            _put(raw, offset, 0x07f0, _level(controls['level']) << 4)
        if 'zone' in controls:
            zone = controls['zone']
            if zone not in HAND_ZONES:
                raise ValueError('zone must be left, right or full')
            if zone != 'full' and not simple_split:
                raise ValueError('Left/right zones require a single middle split')
            _put(raw, offset, 0x7800, HAND_ZONES[zone] << 11)
        for control, delta_bytes in (('wheel_level', 1), ('aftertouch_level', 2), ('control_pedal_level', 3)):
            if control in controls:
                level = (_word(raw, offset) >> 4) & 127
                delta = _level(controls[control]) - level
                code = (128 + delta - 1) if delta > 0 else (127 + delta)
                _put(raw, offset + delta_bytes, 0x0ff0, code << 4)
    # Changing the split must not silently reinterpret another enabled part's zone.
    if split is not None and split['enabled']:
        for name, part in inspect_layout(bytes(raw))['parts'].items():
            if part['enabled'] and part['stored_zone'] not in ('OO--', '--OO', 'OOOO'):
                raise ValueError(f'Explicit left/right/full zone needed for enabled part {name}')
    if header:
        header = bytearray(header)
        struct.pack_into('<I', header, 24, zlib.crc32(raw))
    return bytes(header) + bytes(raw)


def replace_sample(data: bytes, part: str, donor: bytes, donor_part: str) -> bytes:
    """Use a piano/sample identity from another verified program, preserving shaping."""
    supported = ('A.piano', 'B.piano', 'A.synth', 'B.synth')
    if part not in supported or donor_part not in supported or part.split('.')[1] != donor_part.split('.')[1]:
        raise ValueError('Sample source and destination must use the same piano/synth engine')
    header, payload = unpack_program(data)
    _, other = unpack_program(donor)
    target_shift = 263 if part.startswith('B') else 0
    donor_shift = 263 if donor_part.startswith('B') else 0
    raw = bytearray(payload)
    if part.endswith('piano'):
        # Piano type/model, clavinet variation and 32-bit identity, not pedal/timbre.
        masks = {0x48: 0x3f, 0x49: 0xff, 0x4a: 0xff, 0x4b: 0xff, 0x4c: 0xff, 0x4d: 0xf0}
    else:
        for content, shift in ((payload, target_shift), (other, donor_shift)):
            if (_word(content, 0x8d - 44 + shift) >> 7) & 7 != 4:
                raise ValueError('Synth sample selection requires sample mode in both programs')
        masks = {0xa8: 7, 0xa9: 255, 0xaa: 255, 0xab: 255, 0xac: 248}
    for offset, mask in masks.items():
        target = offset - 44 + target_shift
        origin = offset - 44 + donor_shift
        raw[target] = (raw[target] & ~mask) | (other[origin] & mask)
    if header:
        header = bytearray(header)
        struct.pack_into('<I', header, 24, zlib.crc32(raw))
    return bytes(header) + bytes(raw)


def prepare_layout(path: str, changes: dict, directory: str, samples=None) -> dict:
    source = Path(path).resolve()
    if source.suffix.lower() != '.ns3f':
        raise ValueError('Only .ns3f files can be edited')
    before = source.read_bytes()
    after = before
    for sample in samples or []:
        after = replace_sample(after, sample['part'], Path(sample['path']).read_bytes(), sample['source_part'])
    after = edit_layout(after, changes)
    digest = hashlib.sha256(after).hexdigest()
    folder = Path(directory) / digest
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / ('source-' + hashlib.sha256(before).hexdigest() + '.ns3f')
    output = folder / 'layout.ns3f'
    for target, data in ((backup, before), (output, after)):
        if target.exists() and target.read_bytes() != data:
            raise RuntimeError('Saved edit artifact has changed; refusing to overwrite it')
        target.write_bytes(data)
    old_raw, new_raw = unpack_program(before)[1], unpack_program(after)[1]
    report = {'source_path': str(source), 'backup_path': str(backup), 'path': str(output),
              'file_sha256': digest, 'requested_changes': changes,
              'before': inspect_layout(before), 'after': inspect_layout(after),
              'payload_changes': [{'offset': i, 'before': a, 'after': b}
                                  for i, (a, b) in enumerate(zip(old_raw, new_raw)) if a != b],
              'hardware_written': False, 'audio_verified': False,
              'sample_sources': samples or [],
              'limitations': ['Sample choices use existing program references; no sample installation.',
                              'Sample availability has not been verified.',
                              'Unspecified controller offsets are preserved; changing base level can change their targets.',
                              'Only one middle split and left/right/full zones are supported.',
                              'Wheel, aftertouch and control-pedal edits affect part level only; other assignments remain unchanged.']}
    report_path = folder / ('layout-report-' + hashlib.sha256(before).hexdigest() + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    return report
