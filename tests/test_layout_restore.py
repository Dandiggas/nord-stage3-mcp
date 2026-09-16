import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastmcp import Client
import usb.core
import nord_mcp as server
import test_gig_prep
from test_program_edit import UploadKeyboard
import struct


class CategoryKeyboard(UploadKeyboard):
    def __init__(self):
        super().__init__()
        self.categories = {(0,0): 6, (0,1): 3}

    def fileinfo(self, b, s):
        fields, name, tail = super().fileinfo(b, s)
        fields[7] = self.categories.get((b,s), 0)
        return fields, name, tail

    def t(self, msg, payload):
        result = super().t(msg, payload)
        if msg == server.FT_REQ_FILE_CREATE:
            b,s,_,_,_,category = struct.unpack('>6I', payload[:24])
            self.categories[b,s] = category
        elif msg in (server.FT_REQ_FILE_MOVE, server.FT_REQ_FILE_SWAP):
            b,s,db,ds = struct.unpack('>4I',payload)
            original = self.categories.get((b,s),0)
            if msg == server.FT_REQ_FILE_SWAP:
                self.categories[b,s] = self.categories.get((db,ds),0)
            else:
                self.categories.pop((b,s),None)
            self.categories[db,ds] = original
        return result


class RestoreTests(unittest.TestCase):
    def setUp(self):
        test_gig_prep.GigTests.setUp(self)
        self.keyboard = CategoryKeyboard()
        p = patch.object(server, 'PROJECT_DIR', self.temp.name)
        p.start(); self.addCleanup(p.stop)

    def snapshot(self):
        return server.nord_snapshot_layout('Before gig', [0])

    def test_mcp_roundtrip_swap_and_park_preserves_all_contents(self):
        async def flow():
            async with Client(server.mcp) as client:
                snap = (await client.call_tool('nord_snapshot_layout', {'name': 'Before gig', 'banks': [0]})).data
                self.keyboard.files[0, 0], self.keyboard.files[0, 1] = self.keyboard.files[0, 1], self.keyboard.files[0, 0]
                self.keyboard.categories[0,0], self.keyboard.categories[0,1] = self.keyboard.categories[0,1], self.keyboard.categories[0,0]
                self.keyboard.files[0, 2] = ('New patch', b'new-patch')
                self.keyboard.files[1, 0] = ('Keep parking', b'keep-parking')
                before = sorted(self.keyboard.files.values())
                plan = (await client.call_tool('nord_prepare_restore', {'snapshot_id': snap['snapshot_id'], 'parking_banks': [1]})).data
                self.assertTrue(plan['ready'])
                self.assertEqual(self.keyboard.writes, [])
                result = (await client.call_tool('nord_apply_restore', {'restore_id': plan['restore_id'], 'confirm': True})).data
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(self.keyboard.files[0, 0], ('Piano', b'piano-patch'))
                self.assertEqual(self.keyboard.files[0, 1], ('Organ', b'organ-patch'))
                self.assertNotIn((0, 2), self.keyboard.files)
                self.assertEqual(sorted(self.keyboard.files.values()), before)
                count = len(self.keyboard.writes)
                repeat = server.nord_apply_restore(plan['restore_id'], True)
                self.assertTrue(repeat['already_applied'])
                self.assertEqual(len(self.keyboard.writes), count)
        asyncio.run(flow())

    def test_edited_original_recovered_with_category_and_edited_copy_preserved(self):
        snap = self.snapshot()
        self.keyboard.files[0, 0] = ('Piano', b'edited-piano')
        self.keyboard.categories[0,0] = 11
        plan = server.nord_prepare_restore(snap['snapshot_id'], [1])
        self.assertTrue(plan['ready'])
        self.assertEqual([o['kind'] for o in plan['operations']], ['move','upload'])
        result = server.nord_apply_restore(plan['restore_id'], True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.keyboard.files[0,0], ('Piano',b'piano-patch'))
        self.assertEqual(self.keyboard.categories[0,0],6)
        self.assertEqual(self.keyboard.files[1,0], ('Piano',b'edited-piano'))
        self.assertEqual(self.keyboard.categories[1,0],11)

    def test_no_parking_blocks_before_any_write(self):
        snap = self.snapshot()
        self.keyboard.files[0, 2] = ('New', b'new')
        for slot in range(25): self.keyboard.files[1, slot] = ('Full', b'full')
        plan = server.nord_prepare_restore(snap['snapshot_id'], [1])
        self.assertFalse(plan['ready'])
        self.assertEqual(self.keyboard.writes, [])

    def test_missing_original_recovered_into_empty_slot_without_parking(self):
        snap = self.snapshot()
        del self.keyboard.files[0,0]
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        self.assertEqual([o['kind'] for o in plan['operations']], ['upload'])
        async def apply_through_mcp():
            async with Client(server.mcp) as client:
                return (await client.call_tool('nord_apply_restore',
                    {'restore_id':plan['restore_id'], 'confirm':True})).data
        result = asyncio.run(apply_through_mcp())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.keyboard.categories[0,0],6)

    def test_existing_move_plan_without_category_still_applies(self):
        from layout_restore import LayoutRestore
        restore = LayoutRestore(server,Path(self.temp.name)/'layout-snapshots')
        saved = server.nord_prepare_program_space(0,0,2,[1])
        value = restore.load('restore',saved['restore_id'])
        def remove_categories(item):
            if isinstance(item,dict):
                item.pop('category',None)
                for v in item.values(): remove_categories(v)
            elif isinstance(item,list):
                for v in item: remove_categories(v)
        remove_categories(value)
        legacy = restore.store('restore',value)
        result = restore.apply(legacy['restore_id'],True)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(self.keyboard.categories[1,0],6)
        self.assertEqual(self.keyboard.categories[1,1],3)

    def test_legacy_snapshot_without_category_blocks_disk_recovery(self):
        from layout_restore import LayoutRestore
        restore = LayoutRestore(server,Path(self.temp.name)/'layout-snapshots')
        snap = self.snapshot()
        value = restore.load('snapshot',snap['snapshot_id'])
        for row in value['programs']: row.pop('category')
        legacy = restore.store('snapshot',value)
        del self.keyboard.files[0,0]
        plan = restore.prepare(legacy['snapshot_id'],[1])
        self.assertFalse(plan['ready'])
        self.assertIn('category',plan['problems'][0])

    def test_changed_category_blocks_reviewed_plan(self):
        snap = self.snapshot()
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        self.keyboard.categories[0,0] = 11
        with self.assertRaisesRegex(ValueError,'changed'):
            server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(self.keyboard.writes,[])

    def test_undefined_category_is_preserved_during_disk_recovery(self):
        # Real factory programs return word7=0xffffffff; Sound Manager displays
        # this recorded value as Undefined. It is distinct from missing metadata.
        self.keyboard.categories[0,0] = 0xffffffff
        snap = self.snapshot()
        del self.keyboard.files[0,0]
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        self.assertTrue(plan['ready'],plan['problems'])
        result = server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(self.keyboard.files[0,0],('Piano',b'piano-patch'))
        self.assertEqual(self.keyboard.categories[0,0],0xffffffff)

    def test_corrupt_backup_blocks_before_parking(self):
        snap = self.snapshot()
        self.keyboard.files[0,0] = ('Piano',b'edited-piano')
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        Path(snap['programs'][0]['path']).write_bytes(b'corrupt backup')
        with self.assertRaisesRegex(RuntimeError,'Backup'): server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(self.keyboard.writes,[])

    def test_uploaded_category_mismatch_is_detected(self):
        snap = self.snapshot()
        del self.keyboard.files[0,0]
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        original = self.keyboard.t
        def wrong_category(msg,payload):
            result = original(msg,payload)
            if msg == server.FT_REQ_FILE_CREATE: self.keyboard.categories[0,0] = 12
            return result
        with patch.object(self.keyboard,'t',side_effect=wrong_category):
            result = server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(result['status'],'needs_inspection')

    def test_lost_upload_reply_is_never_replayed(self):
        snap = self.snapshot()
        del self.keyboard.files[0,0]
        plan = server.nord_prepare_restore(snap['snapshot_id'],[1])
        original = self.keyboard.t
        def lost(msg,payload):
            result = original(msg,payload)
            if msg == server.FT_REQ_FILE_WRITE: raise usb.core.USBTimeoutError('reply lost')
            return result
        with patch.object(self.keyboard,'t',side_effect=lost):
            result = server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(result['status'],'needs_inspection')
        count = len(self.keyboard.writes)
        with self.assertRaisesRegex(RuntimeError,'inspection'):
            server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(len(self.keyboard.writes),count)

    def test_changed_same_name_content_or_plan_blocks(self):
        snap = self.snapshot()
        plan = server.nord_prepare_restore(snap['snapshot_id'], [1])
        self.keyboard.files[0, 0] = ('Piano', b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'): server.nord_apply_restore(plan['restore_id'], True)
        self.assertEqual(self.keyboard.writes, [])
        path = Path(plan['path']); value = json.loads(path.read_text()); value['banks'] = [1]
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'changed'): server.nord_apply_restore(plan['restore_id'], True)

    def test_lost_reply_is_never_replayed(self):
        snap = self.snapshot()
        self.keyboard.files[0, 2] = ('New', b'new')
        plan = server.nord_prepare_restore(snap['snapshot_id'], [1])
        original = self.keyboard.t
        def lost(msg, payload):
            result = original(msg, payload)
            if msg == server.FT_REQ_FILE_MOVE: raise usb.core.USBTimeoutError('reply lost')
            return result
        with patch.object(self.keyboard, 't', side_effect=lost):
            result = server.nord_apply_restore(plan['restore_id'], True)
        self.assertEqual(result['status'], 'needs_inspection')
        count = len(self.keyboard.writes)
        with self.assertRaisesRegex(RuntimeError, 'inspection'): server.nord_apply_restore(plan['restore_id'], True)
        self.assertEqual(len(self.keyboard.writes), count)

    def test_noop_requires_approval_and_overlapping_parking_rejected(self):
        snap = self.snapshot()
        with self.assertRaises(ValueError): server.nord_prepare_restore(snap['snapshot_id'], [0])
        plan = server.nord_prepare_restore(snap['snapshot_id'], [1])
        with self.assertRaises(ValueError): server.nord_apply_restore(plan['restore_id'])
        self.assertEqual(server.nord_apply_restore(plan['restore_id'], True)['status'], 'complete')
        self.assertEqual(self.keyboard.writes, [])

    def test_make_space_and_restore_snapshot_roundtrip(self):
        snap = self.snapshot()
        original = dict(self.keyboard.files)
        space = server.nord_prepare_program_space(0, 0, 25, [1])
        self.assertTrue(space['ready'])
        self.assertEqual(len(space['operations']), 2)
        self.assertEqual(server.nord_apply_restore(space['restore_id'], True)['status'], 'complete')
        self.assertFalse(any(b == 0 for b,s in self.keyboard.files))
        restore = server.nord_prepare_restore(snap['snapshot_id'], [1])
        self.assertEqual(server.nord_apply_restore(restore['restore_id'], True)['status'], 'complete')
        self.assertEqual(self.keyboard.files, original)

    def test_space_limits_and_capacity_block(self):
        for args in ((0,0,0,[1]), (1,24,2,[0]), (0,0,26,[1])):
            with self.assertRaises(ValueError): server.nord_prepare_program_space(*args)
        for s in range(25): self.keyboard.files[1,s] = ('Full', b'full')
        plan=server.nord_prepare_program_space(0,0,2,[1])
        self.assertFalse(plan['ready'])
        with self.assertRaises(ValueError): server.nord_apply_restore(plan['restore_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_same_size_content_corruption_is_detected_after_move(self):
        plan=server.nord_prepare_program_space(0,0,1,[1])
        original=self.keyboard.t
        def corrupt(msg,payload):
            result=original(msg,payload)
            if msg==server.FT_REQ_FILE_MOVE:
                import struct
                _,_,bank,slot=struct.unpack('>4I',payload)
                name,data=self.keyboard.files[bank,slot]
                self.keyboard.files[bank,slot]=(name,bytes([data[0]^1])+data[1:])
            return result
        with patch.object(self.keyboard,'t',side_effect=corrupt):
            result=server.nord_apply_restore(plan['restore_id'],True)
        self.assertEqual(result['status'],'needs_inspection')
        self.assertIn('contents differ',result['error'])
