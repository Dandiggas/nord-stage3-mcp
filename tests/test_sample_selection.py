import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import nord_mcp as server
from program_edit import replace_sample, unpack_program
from test_layout_edit import program
import test_song_patch


def donor_program():
    raw = bytearray(program())
    for shift in (0, 263):
        raw[0x48-44+shift:0x4e-44+shift] = bytes.fromhex('138123456780')
        offset = 0x8d-44+shift
        raw[offset:offset+2] = (4 << 7).to_bytes(2, 'big')
        raw[0xa8-44+shift:0xad-44+shift] = (0x12345678 << 3).to_bytes(5,'big')
    return bytes(raw)


class SampleTests(unittest.TestCase):
    def test_both_engines_and_panels_preserve_every_unrelated_bit(self):
        donor = donor_program()
        for engine in ('piano','synth'):
            masks = {0x48:63,0x49:255,0x4a:255,0x4b:255,0x4c:255,0x4d:240} if engine=='piano' else {0xa8:7,0xa9:255,0xaa:255,0xab:255,0xac:248}
            for panel,shift in (('A',0),('B',263)):
                for source_panel,source_shift in (('A',0),('B',263)):
                    raw=bytearray(program())
                    for s in (0,263): raw[0x8d-44+s:0x8f-44+s]=(4<<7).to_bytes(2,'big')
                    after=replace_sample(bytes(raw),panel+'.'+engine,donor,source_panel+'.'+engine)
                    for i,(a,b) in enumerate(zip(raw,after)):
                        self.assertEqual((a^b)&~masks.get(i+44-shift,0),0)
                    for offset,mask in masks.items():
                        self.assertEqual(after[offset-44+shift]&mask,donor[offset-44+source_shift]&mask)

    def test_invalid_engine_or_nonsample_synth_rejected(self):
        for part, source in (('A.organ','B.organ'),('A.synth','B.piano'),('A.synth','A.synth')):
            with self.assertRaises(ValueError): replace_sample(program(),part,donor_program(),source)


class SampleWorkflowTests(unittest.TestCase):
    setUp = test_song_patch.SongPatchTests.setUp
    plan = test_song_patch.SongPatchTests.plan

    def checks(self, bank, slot):
        raw=self.keyboard.files[bank,slot][1]
        result=copy.deepcopy(self.dependencies)
        for index,shift in ((0,0),(2,263)):
            start=0x49-44+shift
            sample=(int.from_bytes(raw[start:start+8],'big')>>28)&0xffffffff
            native=((sample+1)&0xffffffff)^0x80000000 if sample else 0
            result['dependencies'][index]['identity_hex']=f'{native:08x}'
            result['dependencies'][index]['name']=f'Piano {sample}'
            start=0xa8-44+shift
            sample=(int.from_bytes(raw[start:start+5],'big')>>3)&0xffffffff
            native=((sample+1)&0xffffffff)^0x80000000 if sample else 0
            result['dependencies'][index+1]['identity_hex']=f'{native:08x}'
        return result

    def test_prepare_apply_remember_new_piano_sample(self):
        self.keyboard.files[0,1]=('Donor',donor_program())
        with patch.object(server,'nord_check_program_dependencies',side_effect=self.checks):
            plan=self.plan(samples={'A.piano':{'bank':0,'slot':1,'part':'B.piano'}})
            self.assertTrue(plan['ready'])
            self.assertNotEqual(plan['sound_check']['dependencies'][0]['identity_hex'],'00000000')
            result=server.nord_apply_song_patch(plan['plan_id'],True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(self.keyboard.files[0,0][1],program())
            self.assertEqual(self.keyboard.files[0,1][1],donor_program())

    def test_changed_or_missing_donor_blocks_before_write(self):
        self.keyboard.files[0,1]=('Donor',donor_program())
        with patch.object(server,'nord_check_program_dependencies',side_effect=self.checks):
            plan=self.plan(samples={'A.piano':{'bank':0,'slot':1,'part':'A.piano'}})
            self.keyboard.files[0,1]=('Donor',program())
            with self.assertRaisesRegex(ValueError,'donor changed'): server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(self.keyboard.writes,[])
        self.keyboard.files[0,1]=('Donor',donor_program())
        def missing(b,s):
            result=self.checks(b,s)
            if (b,s)==(0,1): result['dependencies'][0]['status']='unresolved'
            return result
        with patch.object(server,'nord_check_program_dependencies',side_effect=missing):
            with self.assertRaisesRegex(ValueError,'missing'): self.plan(samples={'A.piano':{'bank':0,'slot':1,'part':'A.piano'}})
        self.assertEqual(self.keyboard.writes,[])

    def test_preview_shows_effects_and_new_sample_name(self):
        self.keyboard.files[0,1]=('Donor',donor_program())
        with patch.object(server,'nord_check_program_dependencies',side_effect=self.checks):
            plan=self.plan(samples={'A.piano':{'bank':0,'slot':1,'part':'A.piano'}},changes={'effects':{'A':{'reverb':{'type':'Hall 2','amount':38}}}})
        preview=Path(plan['preview_path']).read_text()
        self.assertIn('Hall 2',preview)
        self.assertIn(plan['sound_check']['dependencies'][0]['name'],preview)

    def test_dependency_removed_after_review_blocks_apply(self):
        self.keyboard.files[0,1]=('Donor',donor_program())
        with patch.object(server,'nord_check_program_dependencies',side_effect=self.checks):
            plan=self.plan(samples={'A.piano':{'bank':0,'slot':1,'part':'B.piano'}})
        def removed(b,s):
            check=self.checks(b,s)
            if (b,s)==(0,1): check['dependencies'][2]['status']='unresolved'
            return check
        with patch.object(server,'nord_check_program_dependencies',side_effect=removed):
            with self.assertRaisesRegex(ValueError,'missing'): server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(self.keyboard.writes,[])
