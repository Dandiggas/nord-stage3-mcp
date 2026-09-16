import struct
import unittest
import zlib

from program_edit import edit_layout, inspect_layout, unpack_program, REVERB_TYPES
from test_layout_edit import program
import test_program_edit


class EffectsTests(unittest.TestCase):
    def test_both_panels_extremes_and_unrelated_bits(self):
        for panel, shift in (('A', 0), ('B', 263)):
            for amount in (0, 1, 64, 126, 127):
                for kind in ('reverb', 'compressor'):
                    before = program()
                    controls = {'enabled': True, 'amount': amount}
                    controls.update({'type': 'Hall 2', 'bright': True} if kind == 'reverb' else {'fast': True})
                    after = edit_layout(before, {'effects': {panel: {kind: controls}}})
                    effect = inspect_layout(after)['effects'][panel][kind]
                    for key, value in controls.items(): self.assertEqual(effect[key], value)
                    offset = (0x134 if kind == 'reverb' else 0x139) - 44 + shift
                    masks = {offset: 3, offset + 1: 255, offset + 2: 192} if kind == 'reverb' else {offset: 63, offset + 1: 224}
                    for i, (a, b) in enumerate(zip(before, after)):
                        self.assertEqual((a ^ b) & ~masks.get(i, 0), 0)

    def test_reverb_controller_endpoints_and_shared_compressor_bits(self):
        for panel in ('A', 'B'):
            for amount in (0, 64, 127):
                for target in (0, 64, 127):
                    before = edit_layout(program(), {'effects': {panel: {'compressor': {'enabled': True, 'amount': 107, 'fast': True}}}})
                    after = edit_layout(before, {'effects': {panel: {'reverb': {'amount': amount, 'wheel_amount': target, 'aftertouch_amount': target, 'control_pedal_amount': target}}}})
                    effects = inspect_layout(after)['effects'][panel]
                    self.assertEqual(effects['compressor'], inspect_layout(before)['effects'][panel]['compressor'])
                    for value in effects['reverb']['amount_controllers'].values():
                        self.assertEqual(value, {'enabled': target != amount, 'target_amount': target})

    def test_all_reverb_types_and_cbin_checksum(self):
        source = bytearray(program())
        header = bytearray(test_program_edit.EditTests().cbin()[:44])
        struct.pack_into('<I', header, 24, zlib.crc32(source))
        for kind in REVERB_TYPES:
            after = edit_layout(bytes(header) + bytes(source), {'effects': {'B': {'reverb': {'type': kind}}}})
            self.assertEqual(inspect_layout(after)['effects']['B']['reverb']['type'], kind)
            unpack_program(after)

    def test_invalid_controls_fail_closed(self):
        for effects in ({}, {'C': {}}, {'A': {'delay': {}}}, {'A': {'reverb': {'enabled': 1}}},
                        {'A': {'compressor': {'amount': True}}}, {'B': {'reverb': {'amount': 128}}},
                        {'B': {'reverb': {'type': 'Hall 3'}}}, {'A': {'compressor': {'wheel_amount': 30}}}):
            with self.assertRaises(ValueError): edit_layout(program(), {'effects': effects})
