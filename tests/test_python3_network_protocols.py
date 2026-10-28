"""Exercise Python 3 wire/text boundaries without network or device access."""
import ast
import importlib.util
import io
import logging
from pathlib import Path
import struct
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location("test_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def functions(relative, names, namespace):
    """Load selected functions without running service/module startup code."""
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class NetworkTextTests(unittest.TestCase):
    def test_ifconfig_decodes_command_output(self):
        module = load("pair/ifconfig.py")
        output = b"wlan0\n inet addr:192.168.2.207 Bcast:192.168.2.255 Mask:255.255.255.0\n"
        with mock.patch.object(module.subprocess, "check_output", return_value=output):
            self.assertEqual(module.ip_mask("wlan0"), ("192.168.2.207", "255.255.255.0"))
        with mock.patch.object(module.subprocess, "check_output", return_value=b"not configured\xff"):
            self.assertIsNone(module.ip_mask("wlan0"))

    def test_ip_mac_lookup_parses_proc_arp_text(self):
        module = load("pair/ip_util.py")
        table = "IP address HW type Flags HW address Mask Device\n192.168.2.207 0x1 0x2 88:dc:96:3b:aa:30 * wlan0\n"
        with mock.patch("builtins.open", return_value=io.StringIO(table)):
            self.assertEqual(module.get_ip_mac("192.168.2.207"), "88:dc:96:3b:aa:30")

    def test_integer_dhcp_pid_becomes_argument_text(self):
        module = load("pair/udhcpc.py")
        with mock.patch.object(module.subprocess, "check_output", return_value=b"") as command:
            self.assertTrue(module.get_lease(123))
            command.assert_called_once_with(["kill", "-USR1", "123"])

    def test_iw_current_and_older_frequency_output(self):
        module = load("net/usr/bin/iw.py")
        for value in (b"2462", b"2462.0", b"2462.000"):
            with self.subTest(value=value), mock.patch.object(module.subprocess, "check_output", return_value=b"Connected to 00:03:7f:12:c6:27 (on wlan0)\n freq: " + value + b"\n"):
                self.assertEqual(module.getFreq("wlan0"), 2462)
                self.assertEqual(module.freqToChan(module.getFreq("wlan0")), 11)
        with mock.patch.object(module.subprocess, "check_output", return_value=b"Not connected.\n"):
            self.assertIsNone(module.getFreq("wlan0"))


class BinaryProtocolTests(unittest.TestCase):
    def test_fragmented_and_coalesced_input_reports(self):
        module = load("flightcode/stm32/input_report_msg.py")
        first = struct.pack("<IIQHHHH", 2003, 16, 123456, 12, 34, 56, 0)
        second = struct.pack("<IIQHHHH", 2003, 16, 123457, 13, 35, 57, 0)
        sock = mock.Mock()
        sock.recv.side_effect = [first[:7], first[7:] + second, b""]
        self.assertEqual(module.recv(sock), (2003, 123456, 12, 34, 56))
        self.assertEqual(module.recv(sock), (2003, 123457, 13, 35, 57))
        self.assertIsNone(module.recv(sock))
        self.assertEqual(sock.recv.call_count, 3)

    def test_pair_request_layout_matches_documented_offsets(self):
        module = load("pair/wps_pair_msg.py")
        packet = module.pack("02:1F:09:03:00:08", "Solo Mynis", 1)
        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[:12], b"\0\0\0\0\x38\0\0\0\x01\0\0\0")
        self.assertEqual(packet[12:32], b"02:1F:09:03:00:08\0\0\0")
        self.assertEqual(packet[32:42], b"Solo Mynis")
        self.assertEqual(module.unpack(packet), ("02:1F:09:03:00:08", "Solo Mynis", 1))
        self.assertIsNone(module.unpack(packet[:20]))

    def test_solo_versions_decode_packet_text(self):
        files = {}
        def fake_open(path, mode):
            stream = io.StringIO()
            stream.close = lambda: None
            files[path] = stream
            return stream
        ns = functions("pair/pair_solo.py", ["set_controller_versions"], {
            "controller_sololink_version": None, "controller_firmware_version": None,
            "controller_sololink_version_file": "sl", "controller_firmware_version_file": "fw",
            "logger": mock.Mock(), "open": fake_open,
        })
        packet = struct.pack("<BBBB32s32s", 2, 1, 1, 0, b"3.0.0", b"OpenSolo-4dev")
        ns["set_controller_versions"](packet)
        self.assertEqual(ns["controller_sololink_version"], "3.0.0")
        self.assertEqual(files["fw"].getvalue(), "OpenSolo-4dev\n")

    def test_solo_connect_request_and_ack_use_bytes(self):
        pair = load("pair/pair.py")
        class EndIteration(Exception):
            pass
        sock = mock.Mock()
        sock.recvfrom.return_value = (struct.pack("<BBBB32s32s", pair.CMD_CONN_ACK,
                                      pair.SYS_CONTROLLER, pair.YES, 0,
                                      b"3.0.0", b"OpenSolo-4dev"), ("10.1.1.1", 5501))
        ns = functions("pair/pair_solo.py", ["run_connected"], {
            "version_mismatch_log_time_us": 0, "logger": mock.Mock(),
            "controller_ip": "10.1.1.1", "controller_link_port": 5501,
            "socket": types.SimpleNamespace(socket=mock.Mock(return_value=sock), AF_INET=2, SOCK_DGRAM=2, timeout=TimeoutError),
            "connect_ack_timeout": .5, "rc_lock": mock.Mock(locked=lambda: False),
            "struct": struct, "pair": pair,
            "solo_sololink_version": "3.0.0", "solo_firmware_version": "pixhawk",
            "clock": types.SimpleNamespace(gettime_us=lambda clock: 0, CLOCK_MONOTONIC=1),
            "set_controller_versions": mock.Mock(), "check_versions": False,
            "go_ready": mock.Mock(), "connect_request_interval": 1,
            "time": types.SimpleNamespace(sleep=mock.Mock(side_effect=EndIteration)),
        })
        with self.assertRaises(EndIteration):
            ns["run_connected"]()
        packet = sock.sendto.call_args.args[0]
        self.assertEqual(len(packet), 68)
        self.assertEqual(struct.unpack("<BBBB32s32s", packet)[4].rstrip(b"\0"), b"3.0.0")
        ns["set_controller_versions"].assert_called_once()
        ns["go_ready"].assert_called_once()

    def test_pair_button_reads_binary_input_event(self):
        event = struct.pack("@QHHi", 0, 1, 0x211, 1)
        stream = io.BytesIO(event)
        opened = mock.Mock(return_value=stream)
        ns = functions("pair/pair_solo.py", ["pair_button"], {
            "button_error": False, "button_file": None, "button_filename": "/fake/event0",
            "open": opened, "select": types.SimpleNamespace(select=mock.Mock(side_effect=[([stream], [], []), ([], [], [])])),
            "struct": struct, "logger": mock.Mock(),
        })
        self.assertTrue(ns["pair_button"]())
        opened.assert_called_once_with("/fake/event0", "rb", buffering=0)

    def test_gimbal_checksum_keeps_integer_word_count(self):
        module = load("gimbal/firmware_helper.py")
        self.assertEqual(module.bytearray_to_wordarray(bytearray([0x12, 0x34, 0x56, 0x78])), [0x1234, 0x5678])
        binary, checksum = module.append_checksum(bytearray([0x12, 0x34, 0x56, 0x78]))
        self.assertEqual(checksum, 0xbbB3)
        self.assertEqual(binary, [0x34, 0x12, 0x78, 0x56, 0xb3, 0xbb, 0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
