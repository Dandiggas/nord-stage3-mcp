import asyncio
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from fastmcp import Client
import usb.core
import nord_mcp as server
import test_gig_prep
import test_program_edit
from test_layout_edit import program


class SongPatchTests(unittest.TestCase):
    def setUp(self):
        test_gig_prep.GigTests.setUp(self)
        self.keyboard=test_program_edit.UploadKeyboard()
        self.keyboard.files={(0,0):('Source',program())}
        for key,value in [('PROJECT_DIR',self.temp.name)]:
            p=patch.object(server,key,value);p.start();self.addCleanup(p.stop)
        self.dependencies={'all_active_resolved':True,'dependencies':[
            {'active':True,'partition':1,'identity_hex':'00000000','kind':0,'name':'Piano','status':'resolved'},
            {'active':False,'partition':5,'identity_hex':'00000000','kind':3,'name':'','status':'not_required'},
            {'active':False,'partition':1,'identity_hex':'00000000','kind':0,'name':'Piano','status':'resolved'},
            {'active':False,'partition':5,'identity_hex':'00000000','kind':3,'name':'','status':'not_required'},
        ],'blockers':[],'inactive_warnings':[]}
        p=patch.object(server,'nord_check_program_dependencies',side_effect=lambda *_:copy.deepcopy(self.dependencies))
        p.start();self.addCleanup(p.stop)

    def plan(self,**kwargs):
        args=dict(title='Test song',brief='Piano and pad, wheel fade',source_bank=0,source_slot=0,
                  bank=1,slot=0,name='Song Test',changes={'parts':{'A.synth':{'level':0,'wheel_level':127}}})
        args.update(kwargs)
        return server.nord_prepare_song_patch(**args)

    def test_end_to_end_mcp_remembers_and_reuses_song(self):
        async def flow():
            async with Client(server.mcp) as client:
                plan=self.plan()
                self.assertTrue(plan['ready'])
                result=(await client.call_tool('nord_apply_song_patch',{'plan_id':plan['plan_id'],'confirm':True})).data
                self.assertEqual(result['status'],'complete')
                count=len(self.keyboard.writes)
                repeat=(await client.call_tool('nord_apply_song_patch',{'plan_id':plan['plan_id'],'confirm':True})).data
                self.assertTrue(repeat['already_applied'])
                self.assertEqual(len(self.keyboard.writes),count)
            async with Client(server.mcp) as client:
                choices=(await client.call_tool('nord_list_song_choices',{'query':'Test song'})).data
                self.assertEqual(len(choices),1)
                gig=(await client.call_tool('nord_prepare_setlist',{'gig':'Next gig','songs':[{'title':'Test song'}],'start_bank':1,'start_slot':1})).data
                self.assertTrue(gig['ready'])
                self.assertEqual(gig['rows'][0]['source']['slot'],0)
                self.assertEqual(gig['rows'][0]['source']['bank'],1)
                self.assertEqual(self.keyboard.files[0,0][1],program())
        asyncio.run(flow())

    def test_inactive_missing_piano_blocks_when_enabled(self):
        self.dependencies['dependencies'][2].update(status='unresolved',kind=1,name='')
        plan=self.plan(changes={'panels':'AB','parts':{'B.piano':{'enabled':True}}})
        self.assertFalse(plan['ready'])
        self.assertEqual(plan['sound_check']['blockers'][0]['part'],'B.piano')
        self.assertEqual(self.keyboard.writes,[])

    def test_missing_sample_after_approval_blocks_before_upload(self):
        plan=self.plan()
        self.dependencies['dependencies'][0].update(status='unresolved',kind=1,name='')
        with self.assertRaisesRegex(ValueError,'sound'):
            server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(self.keyboard.writes,[])

    def test_approval_tamper_and_collision(self):
        plan=self.plan()
        with self.assertRaisesRegex(ValueError,'approval'):server.nord_apply_song_patch(plan['plan_id'])
        self.keyboard.files[1,0]=('Occupied',program())
        with self.assertRaisesRegex(ValueError,'occupied'):server.nord_apply_song_patch(plan['plan_id'],True)
        self.keyboard.files.pop((1,0))
        Path(plan['edit']['path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'changed'):server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(self.keyboard.writes,[])

    def test_changed_source_and_dependency_identity_rejected(self):
        plan=self.plan()
        self.keyboard.files[0,0]=('Changed',program())
        with self.assertRaisesRegex(ValueError,'Source changed'):server.nord_apply_song_patch(plan['plan_id'],True)
        self.keyboard.files[0,0]=('Source',program())
        self.dependencies['dependencies'][0]['identity_hex']='12345678'
        with self.assertRaisesRegex(ValueError,'identity'):self.plan()

    def test_lost_upload_ack_is_not_replayed_or_remembered(self):
        plan=self.plan();original=self.keyboard.t
        def lose_ack(msg,payload):
            result=original(msg,payload)
            if msg==server.FT_REQ_FILE_WRITE:raise usb.core.USBTimeoutError('write succeeded, reply lost')
            return result
        self.keyboard.t=lose_ack
        result=server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(result['status'],'needs_inspection')
        with self.assertRaisesRegex(RuntimeError,'inspection'):server.nord_apply_song_patch(plan['plan_id'],True)
        self.assertEqual(server.nord_list_song_choices(),[])
        self.assertEqual(self.keyboard.writes.count(server.FT_REQ_FILE_CREATE),1)

    def test_existing_remembered_choice_not_replaced(self):
        server.nord_remember_song('Test song',[{'source_bank':0,'source_slot':0}])
        self.assertFalse(self.plan()['ready'])
        self.assertEqual(self.keyboard.writes,[])
