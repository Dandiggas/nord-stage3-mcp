"""Read-only FileTransfer v10 dependency replies, grounded in NSM disassembly.

CQryFileGetDependency=40, reply=41. Wire format:
0x10008f2b8 (query), 0x100090018 (reply), 0x10008f758 (entries).
"""
import struct


def parse_dependencies(data: bytes, bank: int, slot: int) -> list[dict]:
    position = 0
    def take(size):
        nonlocal position
        if position + size > len(data):
            raise RuntimeError('Truncated dependency reply')
        value = data[position:position + size]
        position += size
        return value
    status, returned_bank, returned_slot, count = struct.unpack('>4I', take(16))
    if status:
        raise RuntimeError(f'Dependency query failed with status {status}')
    if (returned_bank, returned_slot) != (bank, slot):
        raise RuntimeError('Dependency reply address mismatch')
    if count > 8:
        raise RuntimeError('Invalid dependency count')
    rows = []
    for _ in range(count):
        active, kind, partition, identity, length = struct.unpack('>B4I', take(17))
        if active not in (0, 1) or length > 128:
            raise RuntimeError('Invalid dependency flag or name length')
        try:
            name = take(length).decode('ascii')
        except UnicodeDecodeError as error:
            raise RuntimeError('Invalid dependency name encoding') from error
        located, target_bank, target_slot = struct.unpack('>3I', take(12))
        if located not in (0, 1):
            raise RuntimeError('Invalid dependency location flag')
        rows.append({'active': bool(active), 'kind': kind, 'partition': partition,
                     'identity_hex': f'{identity:08x}', 'name': name,
                     'has_location': bool(located), 'bank': target_bank, 'slot': target_slot})
    if position != len(data):
        raise RuntimeError('Unexpected trailing dependency bytes')
    return rows


def resolve_dependencies(rows: list[dict], inventories: dict[int, list[dict]]) -> dict:
    """Confirm keyboard-resolved names against a complete current inventory.

    Identity resolution belongs to the keyboard. Never guess a missing name from
    a catalogue or accept another sample because its name is similar.
    """
    results = []
    for entry in rows:
        row = dict(entry)
        kind = row['kind']
        if kind == 3 and not row['active'] and not row['name']:
            row['status'] = 'not_required'
        elif kind == 1 and not row['name']:
            row['status'] = 'unresolved'
        elif kind != 0 or not row['name'] or row['partition'] not in (1, 5):
            row['status'] = 'unsupported'
        else:
            expected_type = 'npno' if row['partition'] == 1 else 'nsmp'
            matches = [f for f in inventories.get(row['partition'], [])
                       if f['name'] == row['name'] and f['type'] == expected_type]
            if row['has_location']:
                matches = [f for f in matches if (f['bank'], f['slot']) == (row['bank'], row['slot'])]
            row['status'] = 'resolved' if len(matches) == 1 else 'inventory_mismatch'
            row['matches'] = matches
        results.append(row)
    blockers = [r for r in results if r['active'] and r['status'] != 'resolved']
    warnings = [r for r in results if not r['active'] and r['status'] not in ('resolved', 'not_required')]
    return {'dependencies': results, 'all_active_resolved': not blockers,
            'blockers': blockers, 'inactive_warnings': warnings}
