"""Remember explicitly chosen programs without changing the keyboard."""

import fcntl
import json
from pathlib import Path
import re
import unicodedata

from gig_prep import GigPrep, digest, integer, save_json


def text(value, field, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{field} must be {"nonempty " if required else ""}text')
    return value.strip()


def identity(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).casefold())


class SongLibrary:
    def __init__(self, api, path):
        self.api = api
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {'version': 1, 'choices': {}}
        data = json.loads(self.path.read_text())
        if data.get('version') != 1 or not isinstance(data.get('choices'), dict):
            raise ValueError('Song library has an unsupported or damaged format')
        return data

    def list(self, query=''):
        query = identity(text(query, 'query'))
        choices = list(self.read()['choices'].values())
        return sorted((c for c in choices if query in identity(c['title'] + ' ' + c['artist'])),
                      key=lambda c: (c['title'].casefold(), c['artist'].casefold(), c['version'].casefold()))

    def remember(self, title, patches, artist='', version='default', key='', notes='',
                 source_reference='', replace=False):
        title = text(title, 'title', required=True)
        artist = text(artist, 'artist')
        version = text(version, 'version', required=True)
        key, notes = text(key, 'key'), text(notes, 'notes')
        source_reference = text(source_reference, 'source_reference')
        if not identity(title) or not identity(version):
            raise ValueError('Title and version must contain letters or numbers')
        if not isinstance(patches, list) or not 1 <= len(patches) <= 400:
            raise ValueError('Supply 1 to 400 ordered patches')
        choice_id = digest([identity(title), identity(artist), identity(version)])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            if choice_id in data['choices'] and replace is not True:
                raise ValueError('This song/version already has a saved choice; use replace=True to update it')
            inventory = {(f['bank'], f['slot']): f for f in self.api.nord_list_files(7)['files']}
            saved = []
            for patch in patches:
                if not isinstance(patch, dict):
                    raise ValueError('Each patch must be an object')
                bank = integer(patch.get('source_bank'), 'source_bank')
                slot = integer(patch.get('source_slot'), 'source_slot')
                label = text(patch.get('label', ''), 'patch label')
                patch_notes = text(patch.get('notes', ''), 'patch notes')
                current = inventory.get((bank, slot))
                if current is None or current['type'] != 'ns3f':
                    raise ValueError('Choose an occupied Program slot')
                backup = self.api.nord_download_file(7, bank, slot)
                GigPrep.check_backup(backup)
                if backup['name'] != current['name']:
                    raise RuntimeError('Source changed while remembering it; retry')
                saved.append({'source_bank': bank, 'source_slot': slot, 'label': label,
                              'notes': patch_notes, 'name': backup['name'],
                              'sha256': backup['sha256'], 'backup_path': backup['path']})
            choice = {'id': choice_id, 'title': title, 'artist': artist, 'version': version,
                      'key': key, 'notes': notes, 'patches': saved, 'source_reference': source_reference}
            # The saved choice owns a content fingerprint as well as its stable identity.
            choice['revision'] = digest(choice)
            data['choices'][choice_id] = choice
            save_json(self.path, data)
        return dict(choice, path=str(self.path))

    def resolve(self, song):
        """Return an exact remembered choice, or explain why a decision is needed."""
        title = text(song.get('title'), 'title', required=True)
        choices = [c for c in self.read()['choices'].values() if identity(c['title']) == identity(title)]
        for field in ('artist', 'version'):
            if song.get(field):
                wanted = identity(text(song[field], field))
                choices = [c for c in choices if identity(c[field]) == wanted]
        if not choices:
            return None, None
        if song.get('key'):
            key = text(song['key'], 'key')
            matching = [c for c in choices if c['key'].casefold() == key.casefold()]
            if not matching:
                return None, 'Requested key differs from the remembered choice; choose a program explicitly'
            choices = matching
        if len(choices) != 1:
            options = ', '.join(f'{c["artist"] or "unspecified artist"} / {c["version"]}' for c in choices)
            return None, f'Multiple remembered versions match; specify artist/version: {options}'
        choice = choices[0]
        original = {k: v for k, v in choice.items() if k != 'revision'}
        if digest(original) != choice['revision']:
            raise ValueError('Remembered choice was edited outside the tool; save it again explicitly')
        return choice, None
