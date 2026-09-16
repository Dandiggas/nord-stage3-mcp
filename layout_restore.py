"""Backed-up bank snapshots and reviewable, non-erasing layout restoration."""
import fcntl
import html
import json
from pathlib import Path
import re

from gig_prep import GigPrep, digest, integer, save_json
from song_library import text


def location(row):
    return row['bank'], row['slot']


def fingerprint(row):
    return row['name'], row['type'], row['sha256']


def matches(actual, expected):
    return (fingerprint(actual) == fingerprint(expected) and
            ('category' not in expected or actual.get('category') == expected['category']))


def signature(rows):
    return sorted((r['bank'], r['slot'], *fingerprint(r)) for r in rows)


def same_inventory(actual, expected):
    by_location = {location(r): r for r in actual}
    return (signature(actual) == signature(expected) and
            all(matches(by_location[location(r)], r) for r in expected))


def panel_address(pos):
    bank, slot = pos
    return f'{chr(65 + bank)}:{slot // 5 + 1}{slot % 5 + 1}'


class LayoutRestore:
    def __init__(self, api, root):
        self.api, self.root = api, Path(root)

    def banks(self, banks):
        if not isinstance(banks, list) or not banks or len(set(banks)) != len(banks):
            raise ValueError('Provide a nonempty list of distinct bank numbers')
        capacity = {b['bank']: b['capacity'] for b in self.api.nord_list_banks(7)}
        for bank in banks:
            integer(bank, 'bank')
            if bank not in capacity: raise ValueError('Bank outside Program memory')
        return capacity

    def inventory(self):
        before = self.api.nord_list_files(7)['files']
        rows = []
        for row in before:
            saved = self.api.nord_download_file(7, row['bank'], row['slot'])
            GigPrep.check_backup(saved)
            if (saved['name'], saved['type']) != (row['name'], row['type']):
                raise RuntimeError('Inventory changed during backup')
            if saved['type'] != 'ns3f': raise ValueError('Only complete programs are supported')
            rows.append(saved)
        after = self.api.nord_list_files(7)['files']
        listing = lambda rs: sorted((r['bank'], r['slot'], r['name'], r['type']) for r in rs)
        if listing(before) != listing(after): raise RuntimeError('Inventory changed during backup')
        return rows

    def store(self, kind, value):
        ident = digest(value)
        path = self.root / kind / ident / 'plan.json'
        if path.exists() and json.loads(path.read_text()) != value:
            raise ValueError('Saved artifact changed')
        save_json(path, value)
        return dict(value, **{kind + '_id': ident}, path=str(path))

    def load(self, kind, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{64}', ident):
            raise ValueError('Invalid saved ID')
        value = json.loads((self.root / kind / ident / 'plan.json').read_text())
        if digest(value) != ident: raise ValueError('Saved artifact changed')
        return value

    def snapshot(self, name, banks):
        name = text(name, 'name', required=True)
        capacity = self.banks(banks)
        rows = [r for r in self.inventory() if r['bank'] in banks]
        # A second content read catches edits that preserve the name and size.
        for row in rows:
            fresh = self.api.nord_download_file(7, *location(row))
            GigPrep.check_backup(fresh)
            if not matches(fresh, row): raise RuntimeError('Program changed during snapshot')
        return self.store('snapshot', {'version': 2, 'name': name, 'banks': sorted(banks),
            'capacities': {str(b): capacity[b] for b in banks}, 'programs': rows,
            'hardware_written': False,
            'scope': 'Program bank contents and empty slots; samples and instrument settings are not included.'})

    def prepare(self, snapshot_id, parking_banks):
        snap = self.load('snapshot', snapshot_id)
        capacities = self.banks(parking_banks)
        if set(parking_banks) & set(snap['banks']):
            raise ValueError('Parking banks must be outside the restored banks')
        if any(capacities.get(int(b)) != n for b, n in snap['capacities'].items()):
            raise ValueError('Keyboard bank capacities changed')
        for row in snap['programs']: GigPrep.check_backup(row)
        before = self.inventory()
        state = {location(r): r for r in before}
        wanted = {location(r): r for r in snap['programs']}
        operations, problems, fixed = [], [], set()
        def parking():
            return next(((b, s) for b in sorted(parking_banks)
                         for s in range(capacities[b]) if (b, s) not in state), None)
        def move(source, destination, kind):
            operations.append({'kind': kind, 'source': list(source), 'destination': list(destination),
                'source_program': state[source], 'destination_program': state.get(destination)})
            if kind == 'swap': state[source], state[destination] = state[destination], state[source]
            else: state[destination] = state.pop(source)
        for pos, row in sorted(wanted.items()):
            if pos in state and matches(state[pos], row):
                fixed.add(pos)
                continue
            candidates = sorted(p for p, r in state.items() if p not in fixed and matches(r, row))
            if not candidates:
                category = row.get('category')
                # 0xffffffff is the keyboard's valid Undefined category, not
                # absent backup metadata. Preserve it just like named categories.
                if type(category) is not int or not 0 <= category <= 0xffffffff:
                    problems.append(f'{panel_address(pos)} {row["name"]}: backup lacks a verified original category; cannot recover from disk.')
                    continue
                if pos in state:
                    dest = parking()
                    if dest is None:
                        problems.append('Not enough empty parking slots to preserve edited programs')
                        continue
                    move(pos, dest, 'move')
                operations.append({'kind': 'upload', 'source': None, 'destination': list(pos),
                                   'source_program': row, 'destination_program': None})
                state[pos] = row
            else:
                move(candidates[0], pos, 'swap' if pos in state else 'move')
            fixed.add(pos)
        for pos in sorted(p for p in state if p[0] in snap['banks'] and p not in wanted):
            empty = [(b, s) for b in sorted(parking_banks) for s in range(capacities[b]) if (b, s) not in state]
            if not empty:
                problems.append('Not enough empty parking slots to preserve displaced programs')
                break
            move(pos, empty[0], 'move')
        expected = [dict(row, bank=b, slot=s) for (b, s), row in sorted(state.items())]
        plan = {'version': 1, 'snapshot_id': snapshot_id, 'name': snap['name'],
                'banks': snap['banks'], 'parking_banks': sorted(parking_banks),
                'before': before, 'expected': expected, 'operations': operations,
                'ready': not problems, 'problems': problems,
                'scope': 'Restore selected bank layout and original categories using unchanged programs or backed-up files. Preserve all displaced programs. No delete or overwrite.'}
        saved = self.store('restore', plan)
        esc = lambda value: html.escape(str(value))
        rows = ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in (
            op['kind'], panel_address(op['source']) if op['source'] is not None else 'Backup file', op['source_program']['name'],
            op['source_program'].get('category', 'Not recorded'), panel_address(op['destination']),
            (op['destination_program'] or {}).get('name', 'Empty'))) + '</tr>' for op in operations)
        preview = Path(saved['path']).with_name('preview.html')
        preview.write_text('<!doctype html><meta charset="utf-8"><title>Nord restore preview</title>'
            '<style>body{font:17px/1.5 system-ui;max-width:1050px;margin:40px auto}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}table{border-collapse:collapse;width:100%}</style>'
            '<h1>Restore ' + esc(snap['name']) + '</h1><p>' + ('Ready for review' if not problems else 'Blocked') + '</p>'
            '<p>Preserves displaced programs in the listed destinations. No keyboard changes yet.</p>'
            '<ul>' + ''.join('<li>' + esc(p) + '</li>' for p in problems) + '</ul>'
            '<table><tr><th>Action</th><th>From</th><th>Program</th><th>Category ID</th><th>To</th><th>Currently there</th></tr>' + rows + '</table>')
        return dict(saved, preview_path=str(preview))

    def verify(self, plan):
        actual = self.inventory()
        return {'verified': same_inventory(actual, plan['expected']), 'program_count': len(actual)}

    def prepare_space(self, start_bank, start_slot, count, parking_banks):
        capacities = self.banks(parking_banks)
        for value in (start_bank, start_slot, count): integer(value, 'range')
        if count == 0: raise ValueError('Program count must be positive')
        slots = [(b, s) for b in sorted(capacities) for s in range(capacities[b])]
        if (start_bank, start_slot) not in slots: raise ValueError('Start outside Program memory')
        start = slots.index((start_bank, start_slot))
        targets = slots[start:start + count]
        if len(targets) != count: raise ValueError('Requested range exceeds Program memory')
        if set(parking_banks) & {p[0] for p in targets}:
            raise ValueError('Parking banks must be outside the requested range')
        before = self.inventory()
        state = {location(r): r for r in before}
        operations, problems = [], []
        for pos in targets:
            if pos not in state: continue
            empty = [(b, s) for b in sorted(parking_banks) for s in range(capacities[b]) if (b, s) not in state]
            if not empty:
                problems.append('Not enough empty parking slots')
                break
            dest = empty[0]
            operations.append({'kind': 'move', 'source': list(pos), 'destination': list(dest),
                               'source_program': state[pos], 'destination_program': None})
            state[dest] = state.pop(pos)
        plan = {'version': 1, 'name': 'Make room for a gig', 'banks': sorted({p[0] for p in targets}),
                'parking_banks': sorted(parking_banks), 'range': [list(p) for p in targets],
                'before': before, 'expected': [dict(r, bank=b, slot=s) for (b,s),r in sorted(state.items())],
                'operations': operations, 'ready': not problems, 'problems': problems,
                'scope': 'Move occupied range slots to empty parking slots; preserve every program. Stored song choices referencing moved slots will require explicit refresh.'}
        return self.store('restore', plan)

    def apply(self, restore_id, confirm=False):
        if confirm is not True: raise ValueError('Explicit approval of the reviewed restore plan is required')
        plan = self.load('restore', restore_id)
        if not plan['ready']: raise ValueError('Restore plan is blocked')
        folder = self.root / 'restore' / restore_id
        path = folder / 'result.json'
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                previous = json.loads(path.read_text())
                if previous['status'] != 'complete': raise RuntimeError('Previous attempt needs inspection; do not replay')
                check = self.verify(plan)
                return dict(previous, status='complete' if check['verified'] else 'changed', already_applied=True, verification=check)
            for row in plan['before']: GigPrep.check_backup(row)
            for op in plan['operations']:
                GigPrep.check_backup(op['source_program'])
            if not same_inventory(self.inventory(), plan['before']):
                raise ValueError('Keyboard contents changed since review; prepare again')
            journal = {'restore_id': restore_id, 'status': 'applying', 'completed': [], 'path': str(path)}
            save_json(path, journal)
            try:
                for index, op in enumerate(plan['operations']):
                    source, destination = op['source'], op['destination']
                    # Recheck the particular sources immediately before each write.
                    for pos, expected in ((source, op['source_program']), (destination, op['destination_program'])):
                        if pos is None: continue
                        if expected:
                            saved = self.api.nord_download_file(7, *pos)
                            GigPrep.check_backup(saved)
                            if not matches(saved, expected): raise RuntimeError('Restore source changed mid-operation')
                        elif self.api.nord_file_info(7, *pos)['found']:
                            raise RuntimeError('Restore destination became occupied')
                    journal['attempting'] = index
                    save_json(path, journal)
                    if op['kind'] == 'upload':
                        original = op['source_program']
                        GigPrep.check_backup(original)
                        result = self.api.nord_upload_file(7, *destination, original['path'],
                            name=original['name'], attr5=original['category'], confirm=True)
                    else:
                        fn = self.api.nord_swap_files if op['kind'] == 'swap' else self.api.nord_move_file
                        result = fn(7, *source, *destination, confirm=True)
                    if not result.get('verified'): raise RuntimeError('Restore operation not verified')
                    saved = self.api.nord_download_file(7, *destination)
                    GigPrep.check_backup(saved)
                    if not matches(saved, op['source_program']): raise RuntimeError('Restored program contents differ')
                    journal['completed'].append(index)
                    save_json(path, journal)
                journal['verification'] = self.verify(plan)
                if not journal['verification']['verified']: raise RuntimeError('Final layout differs from reviewed plan')
                journal['status'] = 'complete'
            except Exception as error:
                journal.update(status='needs_inspection', error=str(error))
            save_json(path, journal)
            return journal
