import asyncio
import json
from pathlib import Path
import unittest

from fastmcp import Client

import nord_mcp as server
import test_gig_prep


class LibraryTests(unittest.TestCase):
    setUp = test_gig_prep.GigTests.setUp
    plan = test_gig_prep.GigTests.plan

    def remember(self, **overrides):
        args = dict(title='Ballad', patches=[{'source_bank':0, 'source_slot':0}],
                    version='Band', key='Eb', notes='Hold the intro')
        args.update(overrides)
        return server.nord_remember_song(**args)

    def test_remembered_sequence_is_reused_in_another_gig(self):
        choice = self.remember(patches=[{'source_bank':0,'source_slot':0,'label':'Intro'},
                                       {'source_bank':0,'source_slot':1,'label':'Chorus','notes':'Use swell pedal'}])
        self.assertEqual(self.keyboard.writes, [])
        # Each MCP call constructs a fresh library reader; the file owns persistence.
        self.assertEqual(server.nord_list_song_choices('ballad')[0]['id'], choice['id'])
        first = self.plan(songs=[{'title':'Ballad'}])
        second = self.plan(gig='Second gig', songs=[{'title':'Ballad'}])
        for p in (first, second):
            self.assertTrue(p['ready'])
            self.assertEqual([r['source']['name'] for r in p['rows']], ['Piano','Organ'])
            self.assertEqual([r['patch_label'] for r in p['rows']], ['Intro','Chorus'])
            self.assertEqual(p['rows'][0]['key'], 'Eb')
            self.assertIn('Use swell pedal', p['rows'][1]['notes'])
            self.assertEqual(p['rows'][0]['remembered_choice']['revision'], choice['revision'])
        self.assertNotEqual(first['plan_id'], second['plan_id'])

    def test_alternate_versions_need_selection(self):
        self.remember()
        self.remember(version='Acoustic', patches=[{'source_bank':0,'source_slot':1}])
        p = self.plan(songs=[{'title':'Ballad'}])
        self.assertFalse(p['ready'])
        self.assertTrue(any('Multiple remembered versions' in x for x in p['problems']))
        chosen = self.plan(songs=[{'title':'Ballad','version':'Acoustic'}])
        self.assertTrue(chosen['ready'])
        self.assertEqual(chosen['rows'][0]['source']['name'], 'Organ')

    def test_different_artists_are_not_collapsed(self):
        self.remember(artist='Artist One')
        self.remember(artist='Artist Two')
        self.assertFalse(self.plan(songs=[{'title':'Ballad'}])['ready'])
        p = self.plan(songs=[{'title':'Ballad','artist':'Artist Two'}])
        self.assertTrue(p['ready'])
        self.assertEqual(p['rows'][0]['artist'], 'Artist Two')

    def test_key_mismatch_is_not_silently_transposed(self):
        self.remember()
        p = self.plan(songs=[{'title':'Ballad','key':'E'}])
        self.assertFalse(p['ready'])
        self.assertTrue(any('Requested key differs' in x for x in p['problems']))

    def test_changed_remembered_sound_blocks_even_when_name_matches(self):
        self.remember()
        self.keyboard.files[0,0] = ('Piano', b'new-piano')
        p = self.plan(songs=[{'title':'Ballad'}])
        self.assertFalse(p['ready'])
        self.assertTrue(p['rows'][0]['remembered_stale'])
        with self.assertRaises(ValueError):
            server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_missing_remembered_source_blocks(self):
        self.remember()
        self.keyboard.files.pop((0,0))
        self.assertFalse(self.plan(songs=[{'title':'Ballad'}])['ready'])

    def test_explicit_selector_overrides_remembered_choice(self):
        self.remember()
        p = self.plan(songs=[{'title':'Ballad','program':'Organ'}])
        self.assertTrue(p['ready'])
        self.assertEqual(p['rows'][0]['source']['name'], 'Organ')
        self.assertNotIn('remembered_choice', p['rows'][0])

    def test_replacement_is_explicit_and_preserves_alternate(self):
        original = self.remember()
        self.remember(version='Solo')
        with self.assertRaisesRegex(ValueError, 'already has'):
            self.remember(patches=[{'source_bank':0,'source_slot':1}])
        updated = self.remember(patches=[{'source_bank':0,'source_slot':1}], replace=True)
        self.assertNotEqual(original['revision'], updated['revision'])
        self.assertEqual(len(server.nord_list_song_choices()), 2)

    def test_medley_multiple_patches_and_reprise_preserve_order(self):
        self.remember()
        p = self.plan(songs=[{'title':'Opening medley','set':'1','notes':'No gaps', 'sections':[
            {'title':'Ballad'},
            {'title':'Dance','key':'F','patches':[
                {'label':'Verse','program':'Organ'}, {'label':'Chorus','program':'Piano'}]}]},
            {'title':'Ballad','set':'2','notes':'Reprise'}])
        self.assertTrue(p['ready'])
        self.assertEqual([r['title'] for r in p['rows']], ['Ballad','Dance','Dance','Ballad'])
        self.assertEqual([r['song_number'] for r in p['rows']], [1,1,1,2])
        self.assertEqual([r['destination']['address'] for r in p['rows']], ['A25','B01','B02','B03'])
        self.assertIn('No gaps', p['rows'][0]['notes'])
        self.assertIn('Reprise', p['rows'][3]['notes'])
        self.assertEqual(server.nord_apply_setlist(p['plan_id'], True)['status'], 'complete')

    def test_revision_blocks_old_approval_and_shows_changes(self):
        old = self.plan()
        new = self.plan(songs=[{'title':'Organ'}, {'title':'Piano'}], previous_plan_id=old['plan_id'])
        self.assertTrue(new['ready'])
        self.assertEqual(new['previous_plan_id'], old['plan_id'])
        self.assertEqual(len(new['changes']), 3)
        with self.assertRaisesRegex(ValueError, 'superseded'):
            server.nord_apply_setlist(old['plan_id'], True)
        with self.assertRaisesRegex(ValueError, 'approval'):
            server.nord_apply_setlist(new['plan_id'])
        self.assertEqual(self.keyboard.writes, [])
        self.assertEqual(server.nord_apply_setlist(new['plan_id'], True)['status'], 'complete')

    def test_gig_sheet_contains_escaped_notes_and_panel_addresses(self):
        p = self.plan(songs=[{'title':'Piano','notes':'<script>alert(1)</script>'}])
        sheet = Path(p['gig_sheet_path']).read_text()
        self.assertNotIn('<script>', sheet)
        self.assertIn('&lt;script&gt;', sheet)
        self.assertIn('Panel A:55', sheet)
        self.assertIn('Ready for approval', sheet)

    def test_single_remembered_patch_keeps_requested_display_name(self):
        self.remember()
        p = self.plan(songs=[{'title':'Ballad','name':'Show Ballad'}])
        self.assertEqual(p['rows'][0]['name'], 'Show Ballad')

    def test_revision_preserves_email_reference_when_omitted(self):
        old = self.plan(source_reference='Inbox HQ thread 123')
        revised = self.plan(songs=[{'title':'Organ'}], previous_plan_id=old['plan_id'])
        self.assertEqual(revised['source_reference'], 'Inbox HQ thread 123')

    def test_invalid_sequences_and_library_corruption_fail_closed(self):
        for songs in ([{'title':'x','sections':[]}], [{'title':'x','patches':[]}],
                      [{'title':'x','patches':[{}]}], [{'title':'x','sections':[{'title':'y','sections':[]}]}]):
            with self.subTest(songs=songs), self.assertRaises(ValueError):
                self.plan(songs=songs)
        c = self.remember()
        path = Path(c['path'])
        stored = json.loads(path.read_text())
        stored['choices'][c['id']]['patches'][0]['source_slot'] = 1
        path.write_text(json.dumps(stored))
        with self.assertRaisesRegex(ValueError, 'edited outside'):
            self.plan(songs=[{'title':'Ballad'}])

    def test_new_tools_through_mcp(self):
        async def run():
            async with Client(server.mcp) as c:
                saved = (await c.call_tool('nord_remember_song', {'title':'Ballad',
                    'patches':[{'source_bank':0,'source_slot':1}]})).data
                found = (await c.call_tool('nord_list_song_choices', {'query':'Ballad'})).data
                self.assertEqual(found[0]['id'], saved['id'])
                p = (await c.call_tool('nord_prepare_setlist', {'gig':'MCP gig',
                    'songs':[{'title':'Ballad'}], 'start_bank':1})).data
                self.assertTrue(p['ready'])
                self.assertEqual(p['rows'][0]['source']['name'], 'Organ')
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
