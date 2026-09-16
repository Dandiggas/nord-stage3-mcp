import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import usb.core
import nord_mcp as server


class SafetyTests(unittest.TestCase):
    def test_inventory_timeout_is_not_reported_as_complete(self):
        device = Mock()
        device.banklist.return_value = [('A', 25)]
        device.iterate.side_effect = usb.core.USBTimeoutError('interrupted listing')
        with patch.object(server, '_connect', return_value=device), \
             patch.object(server, '_disconnect'), patch.object(server.time, 'sleep'):
            with self.assertRaises(usb.core.USBTimeoutError):
                server.nord_list_files(7)

    def test_swap_is_not_repeated_when_reply_is_lost(self):
        device = Mock()
        device.banklist.return_value = [('A', 25)]
        device.t.side_effect = [usb.core.USBTimeoutError('lost ACK'), struct.pack('>I', 0)]
        with patch.object(server, '_connect', return_value=device), \
             patch.object(server, '_disconnect'), patch.object(server.time, 'sleep'), \
             patch.object(server, '_file_at', side_effect=lambda n,b,s: ('A' if s == 0 else 'B', 'ns3f')):
            with self.assertRaises(usb.core.USBTimeoutError):
                server.nord_swap_files(7, 0, 0, 0, 1, confirm=True)
        self.assertEqual(device.t.call_count, 1)

    def test_download_rejects_wrong_offset_even_if_size_matches(self):
        device = Mock()
        device.fileinfo.return_value = ([0,0,0,4,int.from_bytes(b'ns3f'),0,0,0], 'A', [])
        device.t.side_effect = [struct.pack('>III',0,0,0),
                               struct.pack('>5I',0,0,0,99,4)+b'data', b'']
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, 'DOWNLOADS_DIR', folder), \
             patch.object(server, '_connect', return_value=device):
            with self.assertRaisesRegex(RuntimeError, 'FileRead'):
                server.nord_download_file(7, 0, 0)


if __name__ == '__main__':
    unittest.main()
