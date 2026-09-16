"""Reviewable song patches built from existing Nord programs, with safe recall."""
import fcntl
import copy
import hashlib
import html
import json
from pathlib import Path

from gig_prep import GigPrep, address, digest, integer, save_json
from program_edit import inspect_layout, unpack_program, prepare_layout
from song_library import identity, text


def required_sounds(data, source_check):
    """Re-evaluate requirements after enabling parts that were inactive in the source."""
    _, raw = unpack_program(data)
    layout = inspect_layout(data)
    rows = source_check.get('dependencies', [])
    if len(rows) != 4 or layout['dual_keyboard'] or layout['panels'] == 'unsupported':
        raise ValueError('Unsupported source dependency layout')
    results = []
    for index, (panel, engine) in enumerate((('A','piano'),('A','synth'),('B','piano'),('B','synth'))):
        shift = 263 if panel == 'B' else 0
        part = layout['parts'][panel+'.'+engine]
        panel_active = panel in layout['panels']
        is_sample = engine == 'piano' or ((int.from_bytes(raw[0x8d-44+shift:0x8f-44+shift], 'big') >> 7) & 7) == 4
        needed = panel_active and part['enabled'] and is_sample
        entry = dict(rows[index], part=panel+'.'+engine, active=needed)
        if entry['partition'] != (1 if engine == 'piano' else 5):
            raise ValueError('Unexpected source dependency order')
        if is_sample:
            if engine == 'piano':
                start = 0x49 - 44 + shift
                sample_id = (int.from_bytes(raw[start:start+8], 'big') >> 28) & 0xffffffff
            else:
                start = 0xa8 - 44 + shift
                sample_id = (int.from_bytes(raw[start:start+5], 'big') >> 3) & 0xffffffff
            native_id = (((sample_id + 1) & 0xffffffff) ^ 0x80000000) if sample_id else 0
            if int(entry['identity_hex'],16) != native_id:
                raise ValueError('Program sample identity differs from dependency reply')
        results.append(entry)
    blockers = [r for r in results if r['active'] and r['status'] != 'resolved']
    return {'dependencies':results, 'blockers':blockers, 'all_active_resolved':not blockers,
            'inactive_warnings':[r for r in results if not r['active'] and r['status'] not in ('resolved','not_required')]}


class SongPatch:
    def __init__(self, api, root, library):
        self.api, self.root, self.library = api, Path(root), library

    def _existing_choice(self, song):
        return any(all(identity(c[k]) == identity(song[k]) for k in ('title','artist','version'))
                   for c in self.library.list(song['title']))

    def sample_checks(self, sources, source_check):
        combined = copy.deepcopy(source_check)
        parts = ('A.piano', 'A.synth', 'B.piano', 'B.synth')
        for sample in sources:
            saved = sample['source']
            GigPrep.check_backup(saved)
            fresh = self.api.nord_download_file(7, saved['bank'], saved['slot'])
            GigPrep.check_backup(fresh)
            if (fresh['name'], fresh['sha256']) != (saved['name'], saved['sha256']):
                raise ValueError('Sample donor changed; prepare again')
            check = self.api.nord_check_program_dependencies(saved['bank'], saved['slot'])
            required_sounds(Path(saved['path']).read_bytes(), check)
            row = check['dependencies'][parts.index(sample['source_part'])]
            if row['status'] != 'resolved': raise ValueError('Selected donor sample is missing or not a sample')
            combined['dependencies'][parts.index(sample['part'])] = row
        return combined

    def prepare(self, title, brief, source_bank, source_slot, bank, slot, name, changes,
                artist='', version='default', key='', notes='', samples=None):
        song = {'title':text(title,'title',required=True), 'artist':text(artist,'artist'),
                'version':text(version,'version',required=True), 'key':text(key,'key'),
                'notes':text(notes,'notes'), 'brief':text(brief,'brief',required=True)}
        if not identity(song['title']) or not identity(song['version']):
            raise ValueError('Song title and version must contain letters or numbers')
        if not isinstance(name,str) or not 1 <= len(name) <= 15 or any(not 32 <= ord(c) <= 126 for c in name):
            raise ValueError('Program name must be 1..15 printable ASCII characters')
        for value in (source_bank,source_slot,bank,slot): integer(value,'address')
        banks = self.api.nord_list_banks(7)
        capacities = {b['bank']:b['capacity'] for b in banks}
        if bank not in capacities or slot >= capacities[bank]:
            raise ValueError('Destination is outside Program memory')
        source = self.api.nord_download_file(7,source_bank,source_slot)
        GigPrep.check_backup(source)
        if source['type'] != 'ns3f': raise ValueError('Choose a complete Program')
        donors = []
        if samples is not None:
            if not isinstance(samples, dict) or not samples: raise ValueError('samples must map destination parts to explicit donor programs')
            for part, donor in samples.items():
                valid = ('A.piano','B.piano','A.synth','B.synth')
                if part not in valid or not isinstance(donor, dict) or set(donor) != {'bank','slot','part'}:
                    raise ValueError('Each sample needs a piano/synth destination and donor bank, slot, part')
                if donor['part'] not in valid or donor['part'].split('.')[1] != part.split('.')[1]:
                    raise ValueError('Sample source and destination must use the same engine')
                integer(donor['bank'],'donor bank'); integer(donor['slot'],'donor slot')
                saved = self.api.nord_download_file(7,donor['bank'],donor['slot'])
                GigPrep.check_backup(saved)
                donors.append({'part':part,'source_part':donor['part'],'source':saved})
        edit = prepare_layout(source['path'], changes, str(self.root.parent/'program-edits'),
                              [{'part':d['part'],'source_part':d['source_part'],'path':d['source']['path']} for d in donors]) if donors else self.api.nord_prepare_layout(source['path'],changes)
        source_check = self.api.nord_check_program_dependencies(source_bank,source_slot)
        check = required_sounds(Path(edit['path']).read_bytes(),self.sample_checks(donors,source_check))
        fresh = self.api.nord_download_file(7,source_bank,source_slot)
        if fresh['sha256'] != source['sha256'] or fresh['name'] != source['name']:
            raise RuntimeError('Source changed during preparation')
        inventory = self.api.nord_list_files(7)['files']
        problems=[]
        if any((f['bank'],f['slot']) == (bank,slot) for f in inventory): problems.append('Destination is occupied')
        if any(f['name'] == name for f in inventory): problems.append('Program name already exists')
        if self._existing_choice(song): problems.append('Song/artist/version already remembered; choose a distinct version')
        if not check['all_active_resolved']: problems.append('Required sounds are missing or unresolved after the edit')
        plan = {'version':1, 'song':song, 'source':source, 'sample_sources':donors, 'edit':edit, 'sound_check':check,
                'destination':{'bank':bank,'slot':slot,'address':address(bank,slot),'name':name},
                'upload_category':0, 'ready':not problems, 'problems':problems,
                'note':'Brief is recorded for review, not automatically interpreted. Changes use the chosen existing sounds. Key is a note, not transposition.'}
        plan_id=digest(plan);folder=self.root/plan_id
        save_json(folder/'plan.json',plan)
        esc=lambda v:html.escape(str(v))
        def layout_table(layout):
            split = layout['split']
            points = ', '.join(split[k]['note'] for k in ('low','mid','high') if split[k]['enabled']) if split['enabled'] else 'Off'
            rows = ''
            for part, settings in layout['parts'].items():
                active = settings['enabled'] and part[0] in layout['panels']
                zone = {'OO--':'Left','--OO':'Right','OOOO':'Full keyboard'}.get(settings['stored_zone'],settings['stored_zone'])
                controllers = settings['level_controllers']
                cells = [part.replace('.', ' ').title(), 'On' if active else 'Off', zone, settings['level']]
                cells += [controllers[k]['target_level'] if controllers[k]['enabled'] else 'No level change' for k in ('wheel','control_pedal','aftertouch')]
                rows += '<tr>' + ''.join('<td>'+esc(v)+'</td>' for v in cells) + '</tr>'
            effect_rows = ''
            for panel, sections in layout.get('effects', {}).items():
                for kind, values in sections.items():
                    description = ', '.join(f'{key.replace("_", " ")}: {value}' for key,value in values.items() if key != 'amount_controllers')
                    controllers = values.get('amount_controllers', {})
                    description += ''.join(f', full {key.replace("_", " ")}: {value["target_amount"]}' for key,value in controllers.items() if value['enabled'])
                    effect_rows += '<p>'+esc(panel+' '+kind+': '+description)+'</p>'
            return '<p>Active panels: '+esc(layout['panels'])+'. Split: '+esc(points)+'. Levels: 0–127.</p><table><tr><th>Part</th><th>State</th><th>Range</th><th>Base level</th><th>Full wheel</th><th>Full pedal</th><th>Full aftertouch</th></tr>'+rows+'</table><h3>Effects</h3>'+effect_rows
        sound_rows = ''.join('<li>'+esc(r['part'].replace('.', ' ').title())+': '+esc(r['name'] or 'Unnamed sound')+' ('+esc(r['status'])+')</li>' for r in check['dependencies'] if r['active'])
        sheet=f'''<!doctype html><meta charset="utf-8"><title>{esc(title)} | Nord patch</title>
<style>body{{font:17px/1.5 system-ui;max-width:1100px;margin:40px auto;padding:24px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}h1{{line-height:1.2}}</style>
<h1>{esc(title)}</h1><p>{esc(brief)}</p><p><b>{'Ready for approval' if plan['ready'] else 'Blocked'}</b></p>
<p>From {esc(source['name'])} to {esc(name)}, panel {chr(65+bank)}:{slot//5+1}{slot%5+1}.</p>
<p>{esc('; '.join(problems))}</p><h2>After</h2>{layout_table(edit['after'])}
<h2>Required sounds</h2><ul>{sound_rows}</ul>
<details><summary>Before editing</summary>{layout_table(edit['before'])}</details>
<p>No keyboard write yet. Applying this plan also remembers this song/version. New program category: Acoustic. Audio still needs a listening check.</p>
'''
        (folder/'preview.html').write_text(sheet)
        return dict(plan,plan_id=plan_id,path=str(folder/'plan.json'),preview_path=str(folder/'preview.html'))

    def load(self, plan_id):
        import re
        if not isinstance(plan_id,str) or not re.fullmatch('[0-9a-f]{64}',plan_id): raise ValueError('Invalid plan ID')
        plan=json.loads((self.root/plan_id/'plan.json').read_text())
        if digest(plan)!=plan_id: raise ValueError('Plan changed; prepare a new plan')
        return plan

    def verify(self, plan):
        dst=plan['destination']
        saved=self.api.nord_download_file(7,dst['bank'],dst['slot'])
        GigPrep.check_backup(saved)
        check=self.api.nord_check_program_dependencies(dst['bank'],dst['slot'])
        return {'verified':saved['sha256']==plan['edit']['file_sha256'] and saved['name']==dst['name'] and check['all_active_resolved'],
                'download':saved,'sound_check':check}

    def apply(self, plan_id, confirm=False):
        if confirm is not True: raise ValueError('Explicit approval of the saved song plan is required')
        plan=self.load(plan_id)
        if not plan['ready']: raise ValueError('Song plan is not ready')
        folder=self.root/plan_id;journal_path=folder/'result.json'
        with (folder/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if journal_path.exists():
                previous=json.loads(journal_path.read_text())
                if previous['status']!='complete': raise RuntimeError('Previous attempt needs inspection; do not replay')
                check=self.verify(plan)
                return dict(previous,status='complete' if check['verified'] else 'changed',already_applied=True,verification=check)
            GigPrep.check_backup(plan['source'])
            output=Path(plan['edit']['path']).read_bytes()
            if hashlib.sha256(output).hexdigest()!=plan['edit']['file_sha256']: raise ValueError('Edited file changed')
            source=plan['source'];dst=plan['destination']
            fresh=self.api.nord_download_file(7,source['bank'],source['slot'])
            GigPrep.check_backup(fresh)
            if fresh['sha256']!=source['sha256'] or fresh['name']!=source['name']: raise ValueError('Source changed; prepare again')
            dependencies=self.api.nord_check_program_dependencies(source['bank'],source['slot'])
            if not required_sounds(output,self.sample_checks(plan.get('sample_sources',[]),dependencies))['all_active_resolved']: raise ValueError('Required sound is missing; prepare again')
            if self._existing_choice(plan['song']): raise ValueError('Song/version was remembered since preparation')
            inventory=self.api.nord_list_files(7)['files']
            if any((f['bank'],f['slot'])==(dst['bank'],dst['slot']) or f['name']==dst['name'] for f in inventory):
                raise ValueError('Destination or name is now occupied')
            journal={'plan_id':plan_id,'status':'applying','operation':'upload','path':str(journal_path)}
            save_json(journal_path,journal)
            try:
                result=self.api.nord_upload_file(7,dst['bank'],dst['slot'],plan['edit']['path'],name=dst['name'],attr5=plan['upload_category'],confirm=True)
                if not result.get('verified'): raise RuntimeError('Upload not verified')
                verification=self.verify(plan)
                if not verification['verified']: raise RuntimeError('Saved patch or required sounds failed verification')
                journal.update(operation='remember',verification=verification)
                save_json(journal_path,journal)
                song=plan['song']
                choice=self.library.remember(song['title'],[{'source_bank':dst['bank'],'source_slot':dst['slot']}],
                        song['artist'],song['version'],song['key'],song['notes']+'\n'+song['brief'],
                        'Song patch plan '+plan_id)
                journal.update(status='complete',remembered_choice=choice,operation='complete')
            except Exception as error:
                journal.update(status='needs_inspection',error=str(error))
            save_json(journal_path,journal)
            return journal
