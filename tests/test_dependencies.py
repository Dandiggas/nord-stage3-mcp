import asyncio
import struct
import unittest
from unittest.mock import patch
from fastmcp import Client

from dependencies import parse_dependencies, resolve_dependencies
import nord_mcp as server
import test_gig_prep


def wire(rows, bank=0, slot=0):
    out=struct.pack('>4I',0,bank,slot,len(rows))
    for active,kind,partition,identity,name in rows:
        name=name.encode('ascii')
        out+=struct.pack('>B4I',active,kind,partition,identity,len(name))+name+struct.pack('>3I',0,0xffffffff,0xffffffff)
    return out

ROWS=[(1,0,1,0xdf256d94,'Grand Lady'),(1,0,5,0xd014be94,'Soft Strings'),
      (0,1,1,123,''),(0,3,5,0,'')]
INVENTORY={1:[{'bank':0,'slot':2,'type':'npno','name':'Grand Lady'}],
           5:[{'bank':0,'slot':15,'type':'nsmp','name':'Soft Strings'}]}


class DependencyTests(unittest.TestCase):
    def test_resolved_and_inactive_missing(self):
        rows=parse_dependencies(wire(ROWS),0,0)
        report=resolve_dependencies(rows,INVENTORY)
        self.assertTrue(report['all_active_resolved'])
        self.assertEqual(report['dependencies'][0]['identity_hex'],'df256d94')
        self.assertEqual(report['dependencies'][1]['matches'][0]['slot'],15)
        self.assertEqual(len(report['inactive_warnings']),1)
        self.assertEqual(report['dependencies'][3]['status'],'not_required')

    def test_missing_active_and_similar_names_never_pass(self):
        rows=parse_dependencies(wire(ROWS),0,0)
        rows[1].update(kind=1,name='')
        self.assertFalse(resolve_dependencies(rows,INVENTORY)['all_active_resolved'])
        rows=parse_dependencies(wire(ROWS),0,0)
        for inventory in ({}, {1:INVENTORY[1],5:[dict(INVENTORY[5][0],name='Soft Strings 2')]},
                          {1:INVENTORY[1],5:[dict(INVENTORY[5][0],type='ns3f')]},
                          {1:INVENTORY[1],5:INVENTORY[5]*2}):
            self.assertFalse(resolve_dependencies(rows,inventory)['all_active_resolved'])

    def test_unknown_status_and_active_no_reference_block(self):
        for kind in (2,3,99):
            rows=parse_dependencies(wire(ROWS),0,0);rows[1].update(kind=kind,name='')
            self.assertFalse(resolve_dependencies(rows,INVENTORY)['all_active_resolved'])

    def test_all_truncated_packets_wrong_address_and_counts_rejected(self):
        packet=wire(ROWS)
        for size in range(len(packet)):
            with self.assertRaises(RuntimeError):parse_dependencies(packet[:size],0,0)
        for packet in (packet+b'!',struct.pack('>4I',0,0,0,9),struct.pack('>4I',1,0,0,0),wire(ROWS,1,0)):
            with self.assertRaises(RuntimeError):parse_dependencies(packet,0,0)

    def test_invalid_flags_names_and_location(self):
        packet=bytearray(wire(ROWS));packet[16]=2
        with self.assertRaises(RuntimeError):parse_dependencies(packet,0,0)
        packet=bytearray(wire(ROWS));struct.pack_into('>I',packet,29,129)
        with self.assertRaises(RuntimeError):parse_dependencies(packet,0,0)
        packet=bytearray(wire(ROWS));packet[33]=255
        with self.assertRaises(RuntimeError):parse_dependencies(packet,0,0)

    def test_real_mcp_readonly_check_and_partial_inventory_failure(self):
        keyboard=test_gig_prep.Keyboard()
        original_info=keyboard.fileinfo
        keyboard.fileinfo=lambda b,s: (*original_info(b,s)[:2], [0,0,123])
        calls=[]
        def query(msg,payload):
            calls.append(msg)
            if msg!=40:raise AssertionError('Only dependency reads expected')
            return wire(ROWS)
        keyboard.t=query
        async def flow():
            with patch.object(server,'_connect',lambda:keyboard), patch.object(server,'nord_list_files',lambda partition:{'files':INVENTORY[partition]}):
                async with Client(server.mcp) as client:
                    result=(await client.call_tool('nord_check_program_dependencies',{'bank':0,'slot':0})).data
                    self.assertTrue(result['all_active_resolved'])
                    self.assertFalse(result['hardware_written'])
                self.assertEqual(calls,[40,40])
            with patch.object(server,'_connect',lambda:keyboard), patch.object(server,'nord_list_files',side_effect=RuntimeError('Incomplete inventory')):
                with self.assertRaisesRegex(RuntimeError,'Incomplete'):
                    server.nord_check_program_dependencies(0,0)
        asyncio.run(flow())

    def test_zero_dependencies_cannot_claim_success(self):
        keyboard=test_gig_prep.Keyboard();keyboard.t=lambda *_:wire([])
        with patch.object(server,'_connect',lambda:keyboard):
            with self.assertRaisesRegex(RuntimeError,'Unsupported'):
                server.nord_check_program_dependencies(0,0)
