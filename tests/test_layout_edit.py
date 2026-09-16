import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import nord_mcp as server
from program_edit import edit_layout, inspect_layout, prepare_layout, PART_OFFSETS


def program():
    raw=bytearray(548)
    raw[:5]=bytes.fromhex('0000013011')
    raw[12]=0x30
    for shift in (0,263):
        for offset in PART_OFFSETS.values():
            o=offset+shift
            raw[o:o+5]=bytes.fromhex('cc87f7f7f0') # full zone, level72, zero controller offsets
    return bytes(raw)


class LayoutTests(unittest.TestCase):
    def test_remove_brass_wheel_filter_preserves_other_controls(self):
        for panel, shift in (('A',0),('B',263)):
            before=program()
            after=edit_layout(before,{'parts':{panel+'.synth':{'disable_wheel_filter':True}}})
            offset=0x99-44+shift
            self.assertEqual((int.from_bytes(after[offset:offset+2],'big')>>3)&255,127)
            for i,(a,b) in enumerate(zip(before,after)):
                self.assertEqual((a^b)&~{offset:7,offset+1:248}.get(i,0),0)
            self.assertFalse(inspect_layout(after)['parts'][panel+'.synth']['wheel_filter_enabled'])
        for value in (False,1,'yes'):
            with self.assertRaises(ValueError):
                edit_layout(program(),{'parts':{'A.synth':{'disable_wheel_filter':value}}})
        with self.assertRaises(ValueError):
            edit_layout(program(),{'parts':{'A.piano':{'disable_wheel_filter':True}}})

    def test_levels_for_all_parts_preserve_unrelated_bits(self):
        before=program()
        for panel,shift in (('A',0),('B',263)):
            for engine,base in PART_OFFSETS.items():
                offset=shift+base; name=panel+'.'+engine
                for level in range(128):
                    after=edit_layout(before,{'parts':{name:{'level':level}}})
                    self.assertEqual(inspect_layout(after)['parts'][name]['level'],level)
                    for i,(a,b) in enumerate(zip(before,after)):
                        allowed={offset:7,offset+1:240}.get(i,0)
                        self.assertEqual((a^b)&~allowed,0)

    def test_split_panels_zones_and_controller(self):
        before=program()
        parts={name:{'enabled':False} for name in inspect_layout(before)['parts']}
        parts.update({'A.piano':{'enabled':True,'level':100,'zone':'left'},
                      'B.synth':{'enabled':True,'level':40,'zone':'right','control_pedal_level':110}})
        after=edit_layout(before,{'panels':'AB','split':{'enabled':True,'note':'C4'},'parts':parts})
        layout=inspect_layout(after)
        self.assertEqual(layout['panels'],'AB')
        self.assertEqual(layout['split']['mid'],{'enabled':True,'note':'C4','width':1})
        self.assertFalse(layout['split']['low']['enabled'])
        self.assertFalse(layout['split']['high']['enabled'])
        self.assertEqual(layout['parts']['A.piano']['stored_zone'],'OO--')
        self.assertEqual(layout['parts']['B.synth']['stored_zone'],'--OO')
        self.assertEqual(layout['parts']['B.synth']['level_controllers']['control_pedal'],{'enabled':True,'target_level':110})
        self.assertEqual(after[12],before[12])

    def test_controller_signed_ranges_and_disabled_assignment(self):
        for name in inspect_layout(program())['parts']:
            for level in (0,1,40,126,127):
                for target in (0,1,40,126,127):
                    after=edit_layout(program(),{'parts':{name:{'level':level,'control_pedal_level':target}}})
                    part=inspect_layout(after)['parts'][name]
                    self.assertEqual(part['level_controllers']['control_pedal'],{'enabled':level!=target,'target_level':target})
                    self.assertFalse(part['level_controllers']['wheel']['enabled'])
                    self.assertFalse(part['level_controllers']['aftertouch']['enabled'])

    def test_wheel_fades_strings_from_silence_and_preserves_pedal_top(self):
        original=edit_layout(program(),{'parts':{'A.synth':{'level':70,'control_pedal_level':110}}})
        self.assertEqual(inspect_layout(original)['parts']['A.synth']['level'],70)
        corrected=edit_layout(original,{'parts':{'A.synth':{
            'level':0,'wheel_level':127,'control_pedal_level':110}}})
        part=inspect_layout(corrected)['parts']['A.synth']
        self.assertEqual(part['level'],0)
        self.assertEqual(part['level_controllers']['wheel'],{'enabled':True,'target_level':127})
        self.assertEqual(part['level_controllers']['control_pedal']['target_level'],110)
        self.assertFalse(part['level_controllers']['aftertouch']['enabled'])
        self.assertEqual(inspect_layout(corrected)['parts']['A.piano'],inspect_layout(original)['parts']['A.piano'])

    def test_all_controller_targets_and_parts_preserve_sibling_assignments(self):
        for name in inspect_layout(program())['parts']:
            for control,key in (('wheel_level','wheel'),('aftertouch_level','aftertouch'),('control_pedal_level','control_pedal')):
                for base in (0,1,64,126,127):
                    for target in (0,1,64,126,127):
                        changed=edit_layout(program(),{'parts':{name:{'level':base,control:target}}})
                        part=inspect_layout(changed)['parts'][name]
                        self.assertEqual(part['level_controllers'][key],{'enabled':target!=base,'target_level':target})
                        for sibling in set(part['level_controllers'])-{key}:
                            self.assertFalse(part['level_controllers'][sibling]['enabled'])
                with self.assertRaises(ValueError):
                    edit_layout(program(),{'parts':{name:{control:128}}})

    def test_layers_and_split_widths(self):
        for note in ('F2','C3','F3','C4','F4','C5','F5','C6','F6','C7'):
            for width in (1,6,12):
                after=edit_layout(program(),{'split':{'enabled':True,'note':note,'width':width}})
                self.assertEqual(inspect_layout(after)['split']['mid']['note'],note)
                self.assertEqual(inspect_layout(after)['split']['mid']['width'],width)
                layered=edit_layout(after,{'split':{'enabled':False}})
                self.assertFalse(inspect_layout(layered)['split']['enabled'])

    def test_cbin_layout_updates_checksum_and_preserves_metadata(self):
        import struct,zlib
        header=bytearray(44);header[:4]=b'CBIN';header[8:12]=b'ns3f'
        struct.pack_into('<I',header,4,1);struct.pack_into('<I',header,20,304)
        struct.pack_into('<I',header,24,zlib.crc32(program()))
        before=bytes(header)+program()
        after=edit_layout(before,{'parts':{'B.organ':{'level':22}}})
        self.assertEqual(after[:24],before[:24])
        self.assertEqual(after[28:44],before[28:44])
        self.assertEqual(struct.unpack_from('<I',after,24)[0],zlib.crc32(after[44:]))
        self.assertEqual(inspect_layout(after)['parts']['B.organ']['level'],22)

    def test_invalid_edits_do_not_create_artifacts(self):
        for changes in ({'filter':1},{'panels':'C'},{'split':{'enabled':True,'note':'C#4'}},
                        {'split':{'enabled':True,'note':'C4','width':True}},
                        {'parts':{'C.piano':{'level':2}}},
                        {'parts':{'A.piano':{'zone':'left'}}},
                        {'parts':{'A.piano':{'sample':'new'}}},
                        {'parts':{'A.piano':{'level':128}}},
                        {'parts':{'A.piano':{'control_pedal_level':-1}}},
                        {'parts':{'A.piano':{'enabled':1}}}):
            with tempfile.TemporaryDirectory() as temp:
                source=Path(temp)/'source.ns3f';source.write_bytes(program())
                with self.assertRaises(ValueError): prepare_layout(str(source),changes,temp+'/edits')
                self.assertFalse((Path(temp)/'edits').exists())

    def test_dual_keyboard_and_implicit_rerouting_rejected(self):
        raw=bytearray(program());raw[14]|=8
        with self.assertRaisesRegex(ValueError,'Dual Keyboard'):
            edit_layout(raw,{'panels':'A'})
        raw=bytearray(program());raw[PART_OFFSETS['piano']]&=~0x78
        with self.assertRaisesRegex(ValueError,'Explicit.*zone'):
            edit_layout(raw,{'split':{'enabled':True,'note':'C4'}})

    def test_mcp_prepare_is_offline_and_preserves_backup(self):
        async def flow():
            with tempfile.TemporaryDirectory() as temp, patch.object(server,'PROJECT_DIR',temp), patch.object(server,'_connect',side_effect=AssertionError('No USB expected')):
                source=Path(temp)/'source.ns3f';source.write_bytes(program())
                async with Client(server.mcp) as client:
                    result=(await client.call_tool('nord_prepare_layout',{'path':str(source),'changes':{'parts':{'A.piano':{'level':95}}}})).data
                    self.assertFalse(result['hardware_written'])
                    self.assertEqual(Path(result['backup_path']).read_bytes(),program())
                    inspection=(await client.call_tool('nord_inspect_layout',{'path':result['path']})).data
                    self.assertEqual(inspection['parts']['A.piano']['level'],95)
        asyncio.run(flow())
