import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import nord_mcp as server
import test_gig_prep


class UploadKeyboard(test_gig_prep.Keyboard):
    corrupt = False
    def t(self, msg, payload):
        if msg == server.FT_REQ_FILE_CREATE:
            b,s,size,kind,stamp,attr,length = struct.unpack('>7I', payload[:28])
            self.files[b,s] = (payload[28:28+length].decode(), bytes(size))
            self.writes.append(msg)
            return struct.pack('>I', 0)
        if msg == server.FT_REQ_FILE_WRITE:
            b,s,offset,size = struct.unpack('>4I', payload[:16])
            name,data = self.files[b,s]
            part = payload[16:]
            if self.corrupt:
                part = bytes([part[0] ^ 1]) + part[1:]
            self.files[b,s] = (name,data[:offset]+part+data[offset+size:])
            self.writes.append(msg)
            return struct.pack('>I', 0)
        return super().t(msg,payload)


class UploadTests(unittest.TestCase):
    setUp = test_gig_prep.GigTests.setUp

    def test_legacy_container_upload_preserves_first_twenty_payload_bytes(self):
        keyboard = UploadKeyboard()
        source = Path(self.temp.name)/'legacy.ns3f'
        payload = bytes(range(96))
        header = bytearray(24)
        header[:4] = b'CBIN'
        header[8:12] = b'ns3f'
        struct.pack_into('<I', header, 16, 6)
        source.write_bytes(header + payload)
        with patch.object(server, '_connect', lambda: keyboard):
            result = server.nord_upload_file(7,1,0,str(source),confirm=True)
        self.assertTrue(result['verified'])
        self.assertEqual(keyboard.files[1,0][1], payload)

    def test_container_versions_for_all_supported_sound_types(self):
        for tag in server.TYPE_TO_PARTITION:
            for version, size in ((0,24), (1,44)):
                with self.subTest(tag=tag, version=version):
                    header = bytearray(size)
                    header[:4] = b'CBIN'
                    struct.pack_into('<I',header,4,version)
                    header[8:12] = tag.encode()
                    source = Path(self.temp.name)/('source.'+tag)
                    source.write_bytes(header+b'first sound bytes')
                    self.assertEqual(server._read_upload_source(str(source))['content'], b'first sound bytes')

    def test_unknown_or_truncated_container_is_rejected_before_usb(self):
        source = Path(self.temp.name)/'invalid.ns3f'
        for version,length in ((0,23),(0,24),(1,43),(1,44),(2,80)):
            header = bytearray(length)
            header[:4] = b'CBIN'
            struct.pack_into('<I',header,4,version)
            header[8:12] = b'ns3f'
            source.write_bytes(header)
            with patch.object(server, '_connect') as connect:
                with self.assertRaises(ValueError):
                    server.nord_upload_file(7,1,0,str(source),attr5=6,confirm=True)
                connect.assert_not_called()

    def test_upload_must_read_back_content(self):
        for content in (b'original bytes', EditTests().cbin()):
            keyboard = UploadKeyboard()
            keyboard.corrupt = True
            source = Path(self.temp.name)/'test.ns3f'
            source.write_bytes(content)
            with patch.object(server, '_connect', lambda: keyboard):
                with self.assertRaisesRegex(RuntimeError, 'content'):
                    server.nord_upload_file(7,1,0,str(source),attr5=6,confirm=True)

    def test_upload_success_and_occupied_destination(self):
        keyboard = UploadKeyboard()
        source = Path(self.temp.name)/'test.ns3f'
        source.write_bytes(b'original bytes')
        with patch.object(server, '_connect', lambda: keyboard):
            result=server.nord_upload_file(7,1,0,str(source),attr5=6,confirm=True)
            self.assertTrue(result['verified'])
            self.assertEqual(result['sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
            count=len(keyboard.writes)
            with self.assertRaisesRegex(ValueError,'occupied'):
                server.nord_upload_file(7,1,0,str(source),attr5=6,confirm=True)
            self.assertEqual(len(keyboard.writes),count)

from program_edit import inspect_program, transpose_program, prepare_transpose
import zlib


class EditTests(unittest.TestCase):
    def raw(self):
        data=bytearray((i % 256 for i in range(548)))
        data[:5]=bytes.fromhex('0000013011')
        data[12]=0x3a
        return bytes(data)

    def cbin(self):
        raw=self.raw()
        header=bytearray(44)
        header[:4]=b'CBIN'
        struct.pack_into('<I',header,4,1)
        header[8:12]=b'ns3f'
        struct.pack_into('<I',header,20,304)
        struct.pack_into('<I',header,24,zlib.crc32(raw))
        return bytes(header)+raw

    def test_every_value_preserves_clock_and_other_bytes(self):
        for clock in range(8):
            raw=bytearray(self.raw()); raw[12]=(raw[12]&248)|clock
            for value in range(-6,7):
                for enabled in (False,True):
                    edited=transpose_program(bytes(raw),value,enabled)
                    self.assertEqual(edited[:12],raw[:12])
                    self.assertEqual(edited[13:],raw[13:])
                    self.assertEqual(edited[12]&7,clock)
                    setting=inspect_program(edited)['transpose']
                    self.assertEqual(setting['enabled'],enabled)
                    self.assertEqual(setting['semitones'],value)
                    self.assertEqual(setting['effective_semitones'],value if enabled else 0)

    def test_captured_encoding_values(self):
        # Facts cross-checked against named captures in Chris55/ns3-program-viewer.
        for byte,value,enabled in [(0x38,1,False),(0xb8,1,True),(0x80,-6,True),
                                   (0x88,-5,True),(0xa8,-1,True),(0xd8,5,True),(0xe0,6,True)]:
            raw=bytearray(self.raw()); raw[12]=byte
            self.assertEqual(inspect_program(raw)['transpose']['semitones'],value)
            self.assertEqual(inspect_program(raw)['transpose']['enabled'],enabled)

    def test_cbin_checksum_updated_and_metadata_preserved(self):
        source=self.cbin(); result=transpose_program(source,2)
        self.assertEqual(source[:24],result[:24])
        self.assertEqual(source[28:44],result[28:44])
        self.assertEqual(struct.unpack_from('<I',result,24)[0],zlib.crc32(result[44:]))
        self.assertEqual(inspect_program(result)['transpose']['semitones'],2)

    def test_reject_invalid_inputs(self):
        for value in (-7,7,1.5,True,'2'):
            with self.assertRaises(ValueError): transpose_program(self.raw(),value)
        with self.assertRaises(ValueError): transpose_program(self.raw(),2,1)
        bad_version=bytearray(self.raw()); bad_version[3]=0x2f
        bad_code=bytearray(self.raw()); bad_code[12]=0x78
        bad_type=bytearray(self.cbin()); bad_type[8:12]=b'ns3l'
        legacy=bytearray(self.cbin()); legacy[4]=0
        bad_crc=bytearray(self.cbin()); bad_crc[-1]^=1
        for data in (b'', self.raw()[:-1],bad_version,bad_code,bad_type,legacy,bad_crc):
            with self.assertRaises(ValueError): transpose_program(data,2)

    def test_preparation_preserves_source_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.ns3f'; source.write_bytes(self.raw())
            result=prepare_transpose(str(source),2,True,temp+'/edits')
            self.assertEqual(source.read_bytes(),self.raw())
            self.assertEqual(Path(result['backup_path']).read_bytes(),self.raw())
            self.assertEqual(result['payload_changes'],[{'offset':12,'before':0x3a,'after':0xc2}])
            self.assertFalse(result['hardware_written'])
            Path(result['path']).write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError,'changed'):
                prepare_transpose(str(source),2,True,temp+'/edits')
