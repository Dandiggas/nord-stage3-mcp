"""Saved gig plans built from agent-extracted songs and existing Nord programs."""

import difflib
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, ensure_ascii=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def address(bank, slot):
    return f'{chr(65 + bank)}{slot + 1:02d}'


def normal(text):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower())


def integer(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label} must be a nonnegative integer')
    return value


def render_sheet(plan, plan_id):
    """Printable local report; all email and song text is escaped."""
    def esc(value):
        return html.escape(str(value or ''))
    rows = []
    for row in plan['rows']:
        src = row['source']
        destination = row['destination']
        panel = f'{chr(65 + destination["bank"])}:{destination["slot"] // 5 + 1}{destination["slot"] % 5 + 1}'
        song = esc(row['title'])
        detail = ' / '.join(str(row.get(k) or '') for k in ('medley', 'patch_label') if row.get(k))
        if detail:
            song += '<small>' + esc(detail) + '</small>'
        source = esc(src['name']) + '<small>' + esc(src['address']) + '</small>' if src else '<strong>Choose a sound</strong>'
        if row.get('sound_check', {}).get('all_active_resolved') is False:
            source += '<small>Required sounds need attention</small>'
        if row.get('remembered_stale'):
            source += '<small>Remembered sound has changed</small>'
        rows.append(f'<tr><td>{row["number"]}</td><td>{esc(row.get("set"))}</td><td>{song}</td>'
                    f'<td>{esc(row.get("key"))}</td><td>{source}</td>'
                    f'<td>{esc(destination["address"])}<small>Panel {esc(panel)}</small></td>'
                    f'<td>{esc(row["name"])}</td><td>{esc(row.get("notes")).replace(chr(10), "<br>")}</td></tr>')
    problems = ''.join('<li>' + esc(p) + '</li>' for p in plan['problems'])
    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(plan['gig'])} | Nord gig sheet</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;color:#202020;background:#faf9f6;margin:36px auto;padding:0 24px;max-width:1500px}}
h1{{font-size:32px;margin-bottom:4px}} p{{max-width:1000px}} small{{display:block;color:#555;font-size:12px}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #ddd}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;background:#eee9e1}}td{{font-size:14px}}
.status{{font-weight:700;color:#8b2920}}footer{{margin-top:24px;font-size:12px;overflow-wrap:anywhere}}
@media print{{body{{margin:0;padding:0;background:white}}thead{{display:table-header-group}}tr{{break-inside:avoid}}@page{{size:landscape;margin:12mm}}}}
</style>
<h1>{esc(plan['gig'])}</h1>
<p class="status">{'Ready for approval' if plan['ready'] else 'Draft: choices needed'} · {len(plan['rows'])} program slots</p>
<p>This is a preparation sheet. It does not confirm that programs have been written to the keyboard.
Keys are performance notes; no automatic transposition is applied.</p>
<p>Source: {esc(plan['source_reference'])}</p>
{'<details><summary>'+str(len(plan['problems']))+' choices or slot issues to resolve</summary><ul>'+problems+'</ul></details>' if problems else ''}
<table><thead><tr><th>#</th><th>Set</th><th>Song / section</th><th>Key</th><th>Sound</th><th>Destination</th><th>Keyboard name</th><th>Notes</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<footer>Plan {esc(plan_id)}<br>Review the entire running order before approving. Originals stay in place.</footer></html>'''


class GigPrep:
    def __init__(self, api, root, library=None):
        self.api = api
        self.root = Path(root)
        self.library = library

    def expand(self, songs):
        """Turn songs, medley sections and remembered patch sequences into rows."""
        if not isinstance(songs, list) or not 1 <= len(songs) <= 400:
            raise ValueError('Supply 1 to 400 ordered songs or song sections')
        expanded = []
        for song_number, parent in enumerate(songs, 1):
            if not isinstance(parent, dict) or not isinstance(parent.get('title'), str) or not parent['title'].strip():
                raise ValueError(f'Song {song_number} needs a title')
            sections = parent.get('sections', [parent])
            if not isinstance(sections, list) or not sections:
                raise ValueError('Medley sections must be a nonempty list')
            for section in sections:
                if not isinstance(section, dict) or not isinstance(section.get('title'), str) or not section['title'].strip():
                    raise ValueError('Each medley section needs a title')
                if section is not parent and 'sections' in section:
                    raise ValueError('Nested medleys are not supported; list their sections in order')
                base = {k: v for k, v in section.items() if not k.startswith('_') and k not in ('patches', 'sections')}
                base['song_number'] = song_number
                base['medley'] = parent['title'] if 'sections' in parent else ''
                base['set'] = section.get('set', parent.get('set', ''))
                notes = [str(parent.get('notes') or '')] if section is not parent else []
                notes.append(str(section.get('notes') or ''))
                choice, problem = None, None
                explicit = any(k in section for k in ('program', 'source_bank', 'source_slot', 'patches'))
                if not explicit and self.library is not None:
                    choice, problem = self.library.resolve(section)
                    if choice:
                        base['version'] = choice['version']
                        base['artist'] = choice['artist']
                        base['key'] = section.get('key') or choice['key']
                        base['remembered_choice'] = {'id': choice['id'], 'revision': choice['revision']}
                        notes.append(choice['notes'])
                    elif not problem and (section.get('version') or section.get('artist')):
                        problem = 'No saved choice for this artist/version; choose a program explicitly'
                base['notes'] = '\n'.join(n for n in notes if n)
                patches = section.get('patches', choice['patches'] if choice else [{}])
                if not isinstance(patches, list) or not patches:
                    raise ValueError('patches must be a nonempty ordered list')
                for patch_number, patch in enumerate(patches, 1):
                    if not isinstance(patch, dict):
                        raise ValueError('Each patch must be an object')
                    row = dict(base)
                    if 'patches' in section or choice:
                        for selector in ('program', 'source_bank', 'source_slot', 'name'):
                            row.pop(selector, None)
                        if choice:
                            row.update(source_bank=patch['source_bank'], source_slot=patch['source_slot'])
                            if len(patches) == 1 and 'name' in section:
                                row['name'] = section['name']
                            row['_expected_sha256'] = patch['sha256']
                            row['_expected_name'] = patch['name']
                        else:
                            row.update({k: patch[k] for k in ('program','source_bank','source_slot','name') if k in patch})
                            if not any(k in row for k in ('program','source_bank','source_slot')):
                                raise ValueError('Each explicit patch needs program or source_bank/source_slot')
                    row['patch_label'] = str(patch.get('label') or '')
                    row['patch_number'] = patch_number
                    row['notes'] = '\n'.join(n for n in (base['notes'], str(patch.get('notes') or '')) if n)
                    if problem:
                        row['_problem'] = problem
                    expanded.append(row)
                    if len(expanded) > 400:
                        raise ValueError('Expanded gig exceeds the 400 Program slots')
        return expanded

    def prepare(self, gig, songs, start_bank, start_slot=0, source_reference='', previous_plan_id=None):
        """Read-only on hardware. Songs contain title and optional explicit source."""
        if not isinstance(gig, str) or not gig.strip():
            raise ValueError('gig must have a name')
        if not isinstance(source_reference, str):
            raise ValueError('source_reference must be text')
        previous = self.load(previous_plan_id) if previous_plan_id else None
        if previous and previous['gig'] != gig.strip():
            raise ValueError('A revision must refer to the same gig name')
        if previous and not source_reference:
            source_reference = previous['source_reference']
        original_songs = songs
        songs = self.expand(songs)
        integer(start_bank, 'start_bank')
        integer(start_slot, 'start_slot')
        banks = self.api.nord_list_banks(7)
        slots = [(b['bank'], s) for b in banks for s in range(b['capacity'])]
        if (start_bank, start_slot) not in slots:
            raise ValueError('Starting slot is outside the Program partition')
        offset = slots.index((start_bank, start_slot))
        destinations = slots[offset:offset + len(songs)]
        if len(destinations) != len(songs):
            raise ValueError('Not enough program slots after the starting slot')
        inventory = self.api.nord_list_files(7)['files']
        by_slot = {(f['bank'], f['slot']): f for f in inventory}
        rows, problems = [], []
        names = {f['name'] for f in inventory}
        for number, (song, dst) in enumerate(zip(songs, destinations), 1):
            if not isinstance(song, dict) or not isinstance(song.get('title'), str) or not song['title'].strip():
                raise ValueError(f'Song {number} needs a title')
            title = song['title'].strip()
            src = None
            if 'source_bank' in song or 'source_slot' in song:
                b = integer(song.get('source_bank'), 'source_bank')
                s = integer(song.get('source_slot'), 'source_slot')
                src = by_slot.get((b, s))
            else:
                query = song.get('program', title)
                if not isinstance(query, str) or not normal(query):
                    raise ValueError(f'Song {number} needs a searchable program name')
                matches = [f for f in inventory if normal(f['name']) == normal(query)]
                if len(matches) == 1:
                    src = matches[0]
            label = song.get('name')
            if label is None:
                short = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()
                short = re.sub(r'[^A-Za-z0-9 #b-]', '', short).strip() or 'Song'
                label = f'{number:02d} {short}'[:15]
            if not isinstance(label, str) or not 1 <= len(label) <= 15 or any(ord(c) < 32 or ord(c) > 126 for c in label):
                raise ValueError(f'Song {number}: name must be 1 to 15 printable ASCII characters')
            row = {'number': number, 'title': title, 'key': song.get('key'),
                   'name': label, 'destination': {'bank': dst[0], 'slot': dst[1], 'address': address(*dst)},
                   'source': dict(src, address=address(src['bank'], src['slot'])) if src else None}
            for field in ('song_number', 'medley', 'set', 'notes', 'artist', 'version', 'patch_label', 'patch_number', 'remembered_choice'):
                if field in song:
                    row[field] = song[field]
            if song.get('_problem'):
                row['source'] = None
                src = None
                problems.append(f'{number}. {title}: {song["_problem"]}')
            if src is None:
                candidates = sorted(inventory, key=lambda f: difflib.SequenceMatcher(None, normal(title), normal(f['name'])).ratio(), reverse=True)[:5]
                row['candidates'] = [dict(f, address=address(f['bank'], f['slot'])) for f in candidates]
                problems.append(f'{number}. {title}: choose a source program; suggestions are not selections')
            elif src['type'] != 'ns3f':
                problems.append(f'{number}. {title}: source must be a complete Program')
            if dst in by_slot:
                problems.append(f'{address(*dst)} is occupied by {by_slot[dst]["name"]}')
            if label in names:
                problems.append(f'Program name already in use: {label}')
            names.add(label)
            rows.append(row)
        # Remembered choices are checked even in a partly unresolved draft.
        remembered_backups = {}
        for row, song in zip(rows, songs):
            if row['source'] is None or '_expected_sha256' not in song:
                continue
            src = row['source']
            slot_key = (src['bank'], src['slot'])
            if slot_key not in remembered_backups:
                backup = self.api.nord_download_file(7, *slot_key)
                self.check_backup(backup)
                remembered_backups[slot_key] = backup
            backup = remembered_backups[slot_key]
            if backup['sha256'] != song['_expected_sha256'] or backup['name'] != song['_expected_name']:
                problems.append(f'{row["number"]}. {row["title"]}: remembered program changed; choose or remember it again')
                row['remembered_stale'] = True
        # Even an incomplete gig shows which selected sounds need attention.
        dependency_cache = {}
        for row in rows:
            src = row['source']
            if not src or src['type'] != 'ns3f':
                continue
            key = (src['bank'], src['slot'])
            if key not in dependency_cache:
                try:
                    dependency_cache[key] = self.api.nord_check_program_dependencies(*key)
                except Exception as exc:
                    dependency_cache[key] = {'all_active_resolved': False, 'error': str(exc)}
            row['sound_check'] = dependency_cache[key]
            if row['sound_check'].get('all_active_resolved') is not True:
                detail = row['sound_check'].get('error', 'required sound is missing or unresolved')
                problems.append(f'{row["number"]}. {row["title"]}: sound check failed: {detail}')
        plan = {'version': 3, 'gig': gig.strip(), 'source_reference': source_reference,
                'input_songs': original_songs, 'previous_plan_id': previous_plan_id,
                'rows': rows, 'problems': problems, 'ready': not problems,
                'note': 'Keys are setlist notes, not transposition commands. Existing splits and sounds are copied unchanged.'}
        if not problems:
            # Download and validate every source before presenting the approval plan.
            backups = remembered_backups.copy()
            for row in rows:
                src = row['source']
                key = (src['bank'], src['slot'])
                if key not in backups:
                    backup = self.api.nord_download_file(7, *key)
                    self.check_backup(backup)
                    if backup['name'] != src['name']:
                        raise RuntimeError('Source changed during preparation; prepare again')
                    backups[key] = backup
                row['backup'] = backups[key]
        if previous:
            changes = []
            for number in range(max(len(previous['rows']), len(rows))):
                before = previous['rows'][number] if number < len(previous['rows']) else None
                after = rows[number] if number < len(rows) else None
                if before != after:
                    changes.append({'number': number + 1, 'before': before, 'after': after})
            plan['changes'] = changes
        plan_id = digest(plan)
        folder = self.root / plan_id
        save_json(folder / 'plan.json', plan)
        sheet_path = folder / 'gig-sheet.html'
        sheet_path.write_text(render_sheet(plan, plan_id))
        if previous_plan_id:
            save_json(self.root / previous_plan_id / 'superseded.json', {'new_plan_id': plan_id})
        return dict(plan, plan_id=plan_id, path=str(folder / 'plan.json'), gig_sheet_path=str(sheet_path))

    @staticmethod
    def check_backup(backup):
        data = Path(backup['path']).read_bytes()
        if not backup.get('ok') or len(data) != backup['size'] or hashlib.sha256(data).hexdigest() != backup['sha256']:
            raise RuntimeError('Backup is missing, incomplete or changed')

    def load(self, plan_id):
        if not re.fullmatch(r'[a-f0-9]{64}', plan_id):
            raise ValueError('Invalid plan ID')
        plan = json.loads((self.root / plan_id / 'plan.json').read_text())
        if digest(plan) != plan_id:
            raise ValueError('Saved plan changed; prepare and approve a new plan')
        return plan

    def verify(self, plan):
        results = []
        for row in plan['rows']:
            dst = row['destination']
            actual = self.api.nord_download_file(7, dst['bank'], dst['slot'])
            self.check_backup(actual)
            sound_check = self.api.nord_check_program_dependencies(dst['bank'], dst['slot'])
            ok = actual['name'] == row['name'] and actual['sha256'] == row['backup']['sha256'] and sound_check.get('all_active_resolved') is True
            results.append({'number': row['number'], 'address': dst['address'], 'name': actual['name'],
                            'sha256': actual['sha256'], 'verified': ok, 'sound_check': sound_check})
        return results

    def apply(self, plan_id, confirm=False):
        if confirm is not True:
            raise ValueError('Explicit approval of this saved plan is required; pass confirm=True only after approval')
        plan = self.load(plan_id)
        if (self.root / plan_id / 'superseded.json').exists():
            raise ValueError('This plan was superseded by a revision; review and approve the new plan')
        if not plan['ready']:
            raise ValueError('Plan has unresolved choices; prepare a complete plan first')
        folder = self.root / plan_id
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._apply_locked(plan_id, plan, folder)

    def _apply_locked(self, plan_id, plan, folder):
        journal_path = folder / 'result.json'
        if journal_path.exists():
            previous = json.loads(journal_path.read_text())
            if previous['status'] == 'complete':
                verification = self.verify(plan)
                return {'status': 'complete' if all(r['verified'] for r in verification) else 'changed',
                        'already_applied': True, 'verification': verification, 'path': str(journal_path)}
            raise RuntimeError(f'Previous attempt needs inspection; do not replay. Read {journal_path}')
        # Check the whole batch before the first write, including name collisions.
        inventory = self.api.nord_list_files(7)['files']
        occupied = {(f['bank'], f['slot']) for f in inventory}
        names = {f['name'] for f in inventory}
        for row in plan['rows']:
            self.check_backup(row['backup'])
            dst, src = row['destination'], row['source']
            if (dst['bank'], dst['slot']) in occupied or row['name'] in names:
                raise RuntimeError('Destination or name is now occupied; prepare again')
            current = self.api.nord_download_file(7, src['bank'], src['slot'])
            self.check_backup(current)
            if current['sha256'] != row['backup']['sha256'] or current['name'] != src['name']:
                raise RuntimeError('Source program changed; prepare again')
        # Fresh checks also protect older saved plans and samples removed since approval.
        for source in {(r['source']['bank'], r['source']['slot']) for r in plan['rows']}:
            check = self.api.nord_check_program_dependencies(*source)
            if check.get('all_active_resolved') is not True:
                raise RuntimeError('Required sound is missing or unresolved; prepare again')
        journal = {'plan_id': plan_id, 'status': 'applying', 'steps': [], 'verification': []}
        save_json(journal_path, journal)
        try:
            for row in plan['rows']:
                src, dst = row['source'], row['destination']
                for operation in ('copy', 'rename'):
                    step = {'number': row['number'], 'address': dst['address'], 'operation': operation, 'status': 'attempting'}
                    journal['steps'].append(step)
                    save_json(journal_path, journal)
                    if operation == 'copy':
                        result = self.api.nord_copy_file(7, src['bank'], src['slot'], dst['bank'], dst['slot'], confirm=True)
                    else:
                        result = self.api.nord_rename_file(7, dst['bank'], dst['slot'], row['name'], confirm=True)
                    if not result.get('ok') or result.get('verified') is False:
                        raise RuntimeError(f'{operation} was not verified at {dst["address"]}')
                    step['status'] = 'acknowledged'
                    save_json(journal_path, journal)
                actual = self.api.nord_download_file(7, dst['bank'], dst['slot'])
                self.check_backup(actual)
                if actual['sha256'] != row['backup']['sha256'] or actual['name'] != row['name']:
                    raise RuntimeError(f'Read-back mismatch at {dst["address"]}')
            journal['verification'] = self.verify(plan)
            if not all(r['verified'] for r in journal['verification']):
                raise RuntimeError('Final verification failed')
            journal['status'] = 'complete'
        except Exception as exc:
            journal['status'] = 'needs_inspection'
            journal['error'] = str(exc)
        save_json(journal_path, journal)
        return dict(journal, path=str(journal_path))
