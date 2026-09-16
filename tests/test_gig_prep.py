import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import usb.core

import nord_mcp as server


class Keyboard:
    """Stateful wire-level stand-in, keeping actual MCP and program logic in tests."""
    def __init__(self):
        self.files = {(0, 0): ('Piano', b'piano-patch'), (0, 1): ('Organ', b'organ-patch')}
        self.writes = []
        self.lose_copy_reply = False
        self.ignore_write = False

    def begin(self, p): pass
    def end(self): pass
    def close(self): pass
    def partstate(self, p): return []
    def banklist(self, p): return [('A', 25), ('B', 25)]

    def fileinfo(self, b, s):
        name, data = self.files.get((b, s), ('stale name', b''))
        return [int((b,s) not in self.files),0,0,len(data),int.from_bytes(b'ns3f'),0,0,0], name, []

    def iterate(self, b, s):
        candidates = sorted(slot for bank, slot in self.files if bank == b and (s == 0xFFFFFFFF or slot > s))
        return (0, b, candidates[0]) if candidates else (1, b, 0)

    def t(self, msg, payload):
        if msg == server.FT_REQ_FILE_OPEN:
            return struct.pack('>3I', 0,0,0)
        if msg == server.FT_REQ_FILE_CLOSE:
            return struct.pack('>I', 0)
        if msg == server.FT_REQ_FILE_READ:
            b,s,off,size = struct.unpack('>4I', payload)
            data = self.files[b,s][1][off:off+size]
            return struct.pack('>5I',0,b,s,off,len(data)) + data
        self.writes.append(msg)
        if self.ignore_write:
            return struct.pack('>I',0)
        if msg in (server.FT_REQ_FILE_COPY, server.FT_REQ_FILE_MOVE, server.FT_REQ_FILE_SWAP):
            b,s,db,ds = struct.unpack('>4I', payload)
            if msg == server.FT_REQ_FILE_COPY:
                name,data = self.files[b,s]
                self.files[db,ds] = (name + ' 2', data)
                if self.lose_copy_reply:
                    raise usb.core.USBTimeoutError('copy executed, reply lost')
            elif msg == server.FT_REQ_FILE_MOVE:
                self.files[db,ds] = self.files.pop((b,s))
            else:
                self.files[b,s], self.files[db,ds] = self.files[db,ds], self.files[b,s]
        elif msg == server.FT_REQ_FILE_RENAME:
            b,s,length = struct.unpack('>3I', payload[:12])
            self.files[b,s] = (payload[12:12+length].decode(), self.files[b,s][1])
        else:
            raise AssertionError(f'Unexpected wire command {msg}')
        return struct.pack('>I',0)


class GigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.keyboard = Keyboard()
        for obj, name, value in [
            (server, '_connect', lambda: self.keyboard),
            (server, '_disconnect', lambda: None),
            (server, 'GIG_PLANS_DIR', self.temp.name + '/plans'),
            (server, 'DOWNLOADS_DIR', self.temp.name + '/downloads'),
            (server, 'nord_check_program_dependencies', lambda b,s: {'all_active_resolved':True, 'dependencies':[], 'blockers':[], 'inactive_warnings':[]}),
        ]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def plan(self, **kwargs):
        args = dict(gig='Test gig', songs=[{'title':'Piano'}, {'title':'Organ'}, {'title':'Piano'}], start_bank=0, start_slot=24)
        args.update(kwargs)
        return server.nord_prepare_setlist(**args)

    def test_mcp_flow_repeats_cross_bank_and_restart(self):
        async def flow():
            async with Client(server.mcp) as client:
                tools = {t.name for t in await client.list_tools()}
                self.assertTrue({'nord_prepare_setlist','nord_get_setlist_plan','nord_apply_setlist'} <= tools)
                plan = (await client.call_tool('nord_prepare_setlist', {
                    'gig':'Test', 'songs':[{'title':'Piano'}, {'title':'Organ'}, {'title':'Piano'}],
                    'start_bank':0, 'start_slot':24})).data
                self.assertTrue(plan['ready'])
                self.assertEqual(self.keyboard.writes, [])
                read = (await client.call_tool('nord_get_setlist_plan', {'plan_id':plan['plan_id']})).data
                self.assertEqual(read['gig'], 'Test')
                result = (await client.call_tool('nord_apply_setlist', {'plan_id':plan['plan_id'], 'confirm':True})).data
                self.assertEqual(result['status'], 'complete')
                self.assertEqual([v['address'] for v in result['verification']], ['A25','B01','B02'])
                self.assertTrue(all(v['verified'] for v in result['verification']))
                count = len(self.keyboard.writes)
                again = server.nord_apply_setlist(plan['plan_id'], True)
                self.assertTrue(again['already_applied'])
                self.assertEqual(len(self.keyboard.writes), count)
                self.assertEqual(self.keyboard.files[0,0], ('Piano', b'piano-patch'))
                self.assertEqual(self.keyboard.files[1,1][1], b'piano-patch')
        asyncio.run(flow())

    def test_missing_sounds_block_preparation_and_later_application(self):
        missing={'all_active_resolved':False,'dependencies':[],
                 'blockers':[{'name':'','partition':5,'identity_hex':'1234','status':'unresolved'}],
                 'inactive_warnings':[]}
        with patch.object(server,'nord_check_program_dependencies',return_value=missing):
            plan=self.plan()
            self.assertFalse(plan['ready'])
            self.assertTrue(any('sound' in p.lower() for p in plan['problems']))
        plan=self.plan()
        with patch.object(server,'nord_check_program_dependencies',return_value=missing):
            with self.assertRaisesRegex(RuntimeError,'sound'):
                server.nord_apply_setlist(plan['plan_id'],True)
        self.assertEqual(self.keyboard.writes,[])

    def test_dependency_query_failure_is_not_ready(self):
        with patch.object(server,'nord_check_program_dependencies',side_effect=RuntimeError('USB unavailable')):
            plan=self.plan()
            self.assertFalse(plan['ready'])
            self.assertTrue(any('USB unavailable' in p for p in plan['problems']))

    def test_approval_required(self):
        p = self.plan()
        with self.assertRaisesRegex(ValueError, 'approval'):
            server.nord_apply_setlist(p['plan_id'])
        self.assertEqual(self.keyboard.writes, [])

    def test_unmatched_or_ambiguous_song_blocks(self):
        for duplicate in (False, True):
            if duplicate:
                self.keyboard.files[0,2] = ('Piano', b'other-piano')
            p = self.plan(songs=[{'title': 'Piano' if duplicate else 'unknown'}])
            self.assertFalse(p['ready'])
            self.assertTrue(p['rows'][0]['candidates'])
            with self.assertRaises(ValueError):
                server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_explicit_source_resolves_ambiguity(self):
        self.keyboard.files[0,2] = ('Piano', b'other-piano')
        p = self.plan(songs=[{'title':'Ballad', 'source_bank':0, 'source_slot':2, 'key':'Eb'}])
        self.assertTrue(p['ready'])
        self.assertEqual(p['rows'][0]['backup']['sha256'], hashlib.sha256(b'other-piano').hexdigest())

    def test_occupied_destination_blocks_entire_batch(self):
        p = self.plan()
        self.keyboard.files[1,1] = ('New patch', b'new')
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_source_changed_same_name_blocks(self):
        p = self.plan()
        self.keyboard.files[0,1] = ('Organ', b'edited')
        with self.assertRaisesRegex(RuntimeError, 'Source program changed'):
            server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_changed_plan_and_backup_are_rejected(self):
        p = self.plan()
        Path(p['rows'][0]['backup']['path']).write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError, 'Backup'):
            server.nord_apply_setlist(p['plan_id'], True)
        doc = json.loads(Path(p['path']).read_text())
        doc['rows'][0]['name'] = 'Changed'
        Path(p['path']).write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'Saved plan changed'):
            server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(self.keyboard.writes, [])

    def test_lost_reply_stops_and_cannot_replay(self):
        p = self.plan()
        self.keyboard.lose_copy_reply = True
        r = server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(r['status'], 'needs_inspection')
        self.assertEqual(len(self.keyboard.writes), 1)
        self.assertEqual(r['steps'][0]['status'], 'attempting')
        with self.assertRaisesRegex(RuntimeError, 'do not replay'):
            server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(len(self.keyboard.writes), 1)

    def test_write_ack_without_state_change_fails(self):
        p = self.plan()
        self.keyboard.ignore_write = True
        r = server.nord_apply_setlist(p['plan_id'], True)
        self.assertEqual(r['status'], 'needs_inspection')
        self.assertEqual(len(self.keyboard.writes), 1)

    def test_invalid_destination_and_name(self):
        for args in ({'start_bank':2}, {'start_slot':25}, {'start_bank':-1},
                     {'songs':[{'title':'x', 'name':'bad\x00'}]},
                     {'songs':[{'title':'x','source_bank':0}]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.plan(**args)

    def test_duplicate_backup_names_do_not_overwrite(self):
        self.keyboard.files[0,2] = ('Piano', b'other')
        a = server.nord_download_file(7,0,0)
        b = server.nord_download_file(7,0,2)
        self.assertNotEqual(a['path'], b['path'])
        self.assertEqual(Path(a['path']).read_bytes(), b'piano-patch')


if __name__ == '__main__':
    unittest.main()
