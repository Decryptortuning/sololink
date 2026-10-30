"""Actual bridge paths with local UDP and pipe UART, never physical devices."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('artoo_diagnostics', ROOT/'flightcode/stm32/artoo_diagnostics.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

class DiagnosticsTests(unittest.TestCase):
    def test_snapshot_decoder(self):
        data = bytearray(204)
        struct.pack_into('<BBHHHI', data, 0, 1, 0, 204, 123, 0, 0x20000000)
        self.assertEqual(diag.decode(data,123)['reset_flags'],0x20000000)
        self.assertEqual(diag.decode(data,123)['tick_hz'],1000000000)
        for bad in (data[:-1], data + b'x'):
            with self.assertRaises(ValueError): diag.decode(bad,123)
        with self.assertRaises(ValueError): diag.decode(data,124)
        data[0]=2
        with self.assertRaises(ValueError): diag.decode(data,123)

    def test_fault_record_validation(self):
        words = [0x41525446, 0x00010040] + [0] * 14
        def encoded():
            raw = struct.pack('<16I', *words)
            checksum = 2166136261
            for value in raw[4:60]:
                checksum = ((checksum ^ value) * 16777619) & 0xffffffff
            return raw[:60] + struct.pack('<I', checksum)
        self.assertEqual(diag.validate_fault(encoded())['stage'], 0)
        words[13:15] = [2, 10]
        self.assertEqual(diag.validate_fault(encoded())['stage'], 10)
        damaged = bytearray(encoded()); damaged[20] ^= 1
        with self.assertRaises(ValueError): diag.validate_fault(damaged)
        for flag, stage in [(0, 1), (1, 2), (2, 0), (2, 11), (3, 1), (4, 0)]:
            words[13:15] = [flag, stage]
            with self.assertRaises(ValueError): diag.validate_fault(encoded())
        words[13:15] = [0, 0]
        words[0] = 0
        with self.assertRaises(ValueError): diag.validate_fault(encoded())
        words[0] = 0x41525446; words[1] = 1
        with self.assertRaises(ValueError): diag.validate_fault(encoded())

    def test_actual_bridge_and_telem_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            # Only three external constants are consumed by Telem.cpp; keep all
            # in the stub so a regression to comparing LEN/CRC also compiles.
            header=p/'opensolo/mavlink/solo/mavlink.h'
            header.parent.mkdir(parents=True)
            header.write_text('#define MAVLINK_MSG_ID_LOG_DATA 120\n#define MAVLINK_MSG_ID_LOG_DATA_LEN 97\n#define MAVLINK_MSG_ID_LOG_DATA_CRC 134\n')
            src=ROOT/'flightcode/stm32'
            command=['g++','-std=c++11','-pthread','-funsigned-char','-I'+str(p),'-I'+str(src),
                     '-I'+str(ROOT/'flightcode/util'), str(ROOT/'tests/native/diagnostics_bridge.cpp')]
            command += [str(src/f) for f in ['Diagnostics.cpp','Telem.cpp','PacketHandler.cpp','SLIP.cpp']]
            command += ['-o',str(p/'bridge')]
            subprocess.run(command,check=True,capture_output=True,text=True)
            subprocess.run([str(p/'bridge')],check=True,timeout=10)
