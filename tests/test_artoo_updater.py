"""Updater contract tests: hardware dependencies are replaced before import."""
import importlib.util
import io
import logging
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SERIAL = types.ModuleType('serial')
SERIAL.Serial = Mock(side_effect=AssertionError('Real serial access forbidden'))
with patch.dict(sys.modules, {'serial': SERIAL}), patch.object(sys, 'path', [
        str(ROOT / 'flightcode/python'), str(ROOT / 'flightcode/stm32'), *sys.path]):
    spec = importlib.util.spec_from_file_location('updater_under_test', ROOT / 'STM32Loader/checkArtooAndUpdate.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
CAL = [100, 2000, 4000] * 6


class LoaderBoundaryTests(unittest.TestCase):
    def test_import_never_opens_serial(self):
        SERIAL.Serial.assert_not_called()

    def test_loader_success_and_failed_output_bytes(self):
        for output, success in [(b'\0 verified \xff\n', True),
                (updater.subprocess.CalledProcessError(1, ['fake'], output=b'NACK\xff\n'), False)]:
            with self.subTest(success=success), patch.object(updater.subprocess, 'check_output') as call, \
                    patch.object(updater.time, 'sleep'), patch.object(updater.logger, 'info') as info:
                if isinstance(output, Exception):
                    call.side_effect = output
                else:
                    call.return_value = output
                self.assertIs(updater.call_stm32loader('candidate.bin'), success)
                self.assertNotIn('u', call.call_args.args[0][1])
                self.assertTrue(any('\ufffd' in str(args) for args, _ in info.call_args_list))

    def test_calibration_rejects_missing_erased_wrong_shape_and_ranges(self):
        for value in [None, [], [1]*17, [0xffff]*18, [3, 2, 1]*6, [0, 0, 0]*6, [True, 2, 3]*6]:
            with self.subTest(value=value), self.assertRaises(updater.UpdateError):
                updater.validate_stick_cal(value)
        self.assertEqual(updater.validate_stick_cal(CAL), CAL)

    def test_factory_linear_rate_trim_preserved_and_angular_rules_retained(self):
        factory = [733,2299,3701,806,2261,3699,778,2304,3719,754,2127,3511,896,2024,3257,1191,0,2751]
        self.assertEqual(updater.validate_stick_cal(factory), factory)
        unused = factory.copy(); unused[16] = 65535
        self.assertEqual(updater.validate_stick_cal(unused), unused)
        for axis in range(5):
            bad = factory.copy(); bad[axis*3+1] = 0
            with self.assertRaises(updater.UpdateError): updater.validate_stick_cal(bad)
        for low, high in [(2000,1000),(1000,1000),(0,4096),(65535,65535)]:
            bad = factory.copy(); bad[15],bad[17] = low,high
            with self.assertRaises(updater.UpdateError): updater.validate_stick_cal(bad)

    def test_calibration_write_uses_wire_bytes_and_closes_uart(self):
        port = Mock()
        slip = Mock()
        with patch.object(updater.serial, 'Serial', return_value=port), \
                patch.object(updater.slip, 'SlipDevice', return_value=slip):
            updater.write_stick_cal(CAL)
        slip.write.assert_called_once_with(b'\x02' + struct.pack('<18H', *CAL))
        port.close.assert_called_once()

    def test_calibration_read_always_reaps_owned_child(self):
        process = Mock()
        with patch.object(updater.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(updater.time, 'sleep'), patch.object(updater.param_stored_vals_msg, 'fetch', side_effect=RuntimeError('receive failed')):
            with self.assertRaises(RuntimeError):
                updater.read_stick_cal()
        spawn.assert_called_once_with(['stm32'])
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_short_sysinfo_packet_is_ignored_and_uart_closed(self):
        port = Mock()
        slip = Mock()
        slip.read.side_effect = [list('\x03short'), list('\x03'+'\x00'*14+'v4.0.0')]
        with patch.object(updater.serial, 'Serial', return_value=port), \
                patch.object(updater.slip, 'SlipDevice', return_value=slip):
            self.assertEqual(updater.getArtooVersion(), '4.0.0')
        port.close.assert_called_once()

    def test_startup_noise_many_unrelated_packets_and_nul_version(self):
        port = Mock()
        frames = [[], list('\x03short')] + [list('\x01input')] * 40
        frames += [list('\x03' + '\x00' * 14 + 'bad\x01version'),
                   list('\x03' + '\x00' * 14 + 'v5.0.0-dev+bench\0')]
        device = Mock(); device.read.side_effect = frames
        with patch.object(updater.serial, 'Serial', return_value=port), \
                patch.object(updater.slip, 'SlipDevice', return_value=device):
            self.assertEqual(updater.getArtooVersion(), '5.0.0-dev+bench')
        port.reset_input_buffer.assert_called_once()
        port.close.assert_called_once()
        self.assertGreater(device.read.call_count, 5)

    def test_identification_deadline_and_no_programming(self):
        port = Mock(); device = Mock(); device.read.return_value = []
        ticks = iter(range(100))
        with patch.object(updater.serial, 'Serial', return_value=port), \
                patch.object(updater.slip, 'SlipDevice', return_value=device), \
                patch.object(updater.time, 'monotonic', side_effect=lambda: next(ticks)), \
                patch.object(updater, 'call_stm32loader') as loader:
            self.assertEqual(updater.getArtooVersion(timeout=4), 'unknown')
        loader.assert_not_called(); port.close.assert_called_once()

    def test_real_slip_oversize_then_reply_recovers(self):
        class Port:
            def __init__(self):
                self.data = iter(b'\xc0' + b'x'*5000 + b'\xc0\x03valid\xc0')
            def read(self):
                return bytes([next(self.data)])
        device = updater.slip.SlipDevice(Port())
        self.assertEqual(device.read(), [])
        self.assertFalse(device.insync)
        self.assertEqual(''.join(device.read()), '\x03valid')

    def test_real_slip_continuous_unframed_traffic_is_time_bounded(self):
        class Port:
            def read(self): return b'x'
        device = updater.slip.SlipDevice(Port())
        clock = iter(range(100))
        with patch.object(updater.slip.time, 'monotonic', side_effect=lambda: next(clock)):
            self.assertEqual(device.read(deadline=3), [])
        self.assertFalse(device.insync)

    def test_programming_requires_bound_power_hold_driver(self):
        with patch.object(updater.os.path, 'islink', return_value=False):
            with self.assertRaises(updater.UpdateError): updater.require_controller_power_hold()
        with patch.object(updater.os.path, 'islink', return_value=True), \
                patch.object(updater.os.path, 'realpath', return_value='/sys/bus/platform/drivers/opensolo-controller-power'):
            updater.require_controller_power_hold()
        with patch.object(updater.os.path, 'islink', return_value=True), \
                patch.object(updater.os.path, 'realpath', return_value='/wrong-driver'):
            with self.assertRaises(updater.UpdateError): updater.require_controller_power_hold()


class ProgrammingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.firmware = self.root / 'artoo_4.0.0.bin'
        self.firmware.write_bytes(b'candidate')
        self.settings = patch.object(updater, 'UPDATE_DIR', str(self.root / 'updates'))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        hold = patch.object(updater, 'require_controller_power_hold')
        hold.start(); self.addCleanup(hold.stop)

    def test_missing_power_driver_blocks_before_calibration_or_flash(self):
        with patch.object(updater, 'require_controller_power_hold', side_effect=updater.UpdateError('unbound')), \
                patch.object(updater, 'read_stick_cal') as calibration, \
                patch.object(updater, 'call_stm32loader') as loader:
            with self.assertRaises(updater.UpdateError):updater.updateStm32(str(self.firmware))
        calibration.assert_not_called();loader.assert_not_called()

    def test_success_preserves_and_durably_records_calibration(self):
        with patch.object(updater, 'read_stick_cal', return_value=CAL), \
                patch.object(updater, 'call_stm32loader', return_value=True) as loader:
            self.assertIs(updater.updateStm32(str(self.firmware)), True)
        loader.assert_called_once_with(str(self.firmware))
        backups = list((self.root/'updates').glob('artoo-calibration-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_no_programming_without_calibration_or_durable_backup(self):
        for value in [None, [0xffff]*18]:
            with self.subTest(value=value), patch.object(updater, 'read_stick_cal', return_value=value), \
                    patch.object(updater, 'call_stm32loader') as loader, self.assertRaises(updater.UpdateError):
                updater.updateStm32(str(self.firmware))
            loader.assert_not_called()
        with patch.object(updater, 'read_stick_cal', return_value=CAL), \
                patch.object(updater, 'backup_stick_cal', side_effect=OSError('disk full')), \
                patch.object(updater, 'call_stm32loader') as loader, self.assertRaises(OSError):
            updater.updateStm32(str(self.firmware))
        loader.assert_not_called()

    def test_exhaustion_never_escalates_to_mass_erase(self):
        with patch.object(updater, 'read_stick_cal', return_value=CAL), \
                patch.object(updater, 'call_stm32loader', return_value=False) as loader, \
                self.assertRaisesRegex(updater.UpdateError, 'no automatic retry or unprotect attempted'):
            updater.updateStm32(str(self.firmware))
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(not call.kwargs for call in loader.call_args_list))

    def test_calibration_restore_requires_readback(self):
        with patch.object(updater, 'read_stick_cal', side_effect=[CAL, None, CAL]), \
                patch.object(updater, 'call_stm32loader', return_value=True), \
                patch.object(updater, 'write_stick_cal') as restore, patch.object(updater.time, 'sleep'):
            self.assertTrue(updater.updateStm32(str(self.firmware)))
        restore.assert_called_once_with(CAL)
        with patch.object(updater, 'read_stick_cal', side_effect=[CAL, None, None, None, None]), \
                patch.object(updater, 'call_stm32loader', return_value=True), \
                patch.object(updater, 'write_stick_cal') as restore, patch.object(updater.time, 'sleep'), \
                self.assertRaises(updater.UpdateError):
            updater.updateStm32(str(self.firmware))
        self.assertEqual(restore.call_count, 3)

    def test_empty_or_oversized_firmware_is_rejected_before_calibration(self):
        for size in [0, 0x3f801]:
            self.firmware.write_bytes(b'X'*size)
            with patch.object(updater, 'read_stick_cal') as read, self.assertRaises(updater.UpdateError):
                updater.updateStm32(str(self.firmware))
            read.assert_not_called()

    def run_scenario(self, version='3.0.0', candidate='4.0.0', enabled=True, force=False, result=True, after='4.0.0'):
        stack = __import__('contextlib').ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(updater, 'getFirmwareInfo', return_value=(str(self.firmware), candidate)))
        stack.enter_context(patch.object(updater, 'getArtooVersion', side_effect=[version, after]))
        stack.enter_context(patch.object(updater, '_update_enabled', return_value=enabled))
        stack.enter_context(patch.object(updater, '_force_update_enabled', return_value=force))
        stack.enter_context(patch.object(updater, 'writeVersionFile'))
        stack.enter_context(patch.object(updater, 'doUpdateComplete'))
        loader = stack.enter_context(patch.object(updater, 'updateStm32', return_value=result))
        return loader

    def test_disabled_same_newer_and_unparseable_candidates_are_retained(self):
        for args in [dict(enabled=False), dict(version='4.0.0'), dict(version='5.0.0'), dict(version='OpenSolo-4dev')]:
            with self.subTest(args=args):
                loader = self.run_scenario(**args)
                self.assertEqual(updater.run_update(), 0)
                loader.assert_not_called()
                self.assertTrue(self.firmware.exists())

    def test_success_archives_only_after_exact_running_version_matches(self):
        self.run_scenario()
        self.assertEqual(updater.run_update(), 0)
        self.assertFalse(self.firmware.exists())
        self.assertTrue((self.root/'loaded'/self.firmware.name).exists())

    def test_failure_or_version_mismatch_retains_candidate(self):
        for args in [dict(result=False), dict(after='3.0.0'), dict(after='4.0.0-dev+other')]:
            with self.subTest(args=args):
                self.run_scenario(**args)
                with self.assertRaises(updater.UpdateError):
                    updater.run_update()
                self.assertTrue(self.firmware.exists())

    def test_forced_candidate_still_requires_verified_running_version(self):
        loader = self.run_scenario(version='OpenSolo-4dev', force=True, after='bad')
        with self.assertRaises(updater.UpdateError):
            updater.run_update()
        loader.assert_called_once()
        self.assertTrue(self.firmware.exists())

    def test_no_candidate_boot_does_not_program(self):
        with patch.object(updater, 'getFirmwareInfo', return_value=None), \
                patch.object(updater, 'getArtooVersion', return_value='OpenSolo-4dev'), \
                patch.object(updater, 'updateStm32') as flash, patch.object(updater, 'writeVersionFile') as version, \
                patch.object(updater, 'doUpdateComplete'):
            self.assertEqual(updater.run_update(), 0)
        flash.assert_not_called()
        version.assert_called_once_with('OpenSolo-4dev')

    def test_main_failure_records_marker_and_display_even_if_ready_exists(self):
        (self.root/'updates').mkdir()
        (self.root/'updates'/'READY').touch()
        with patch.object(updater.logging.config, 'fileConfig'), \
                patch.object(updater, 'run_update', side_effect=updater.UpdateError('verification failed')), \
                patch.object(updater, 'setArtooUpdateComplete') as display:
            self.assertEqual(updater.main(), 1)
        display.assert_called_once_with(updater.ARTOO_UPDATE_FAILED)
        self.assertIn('verification failed', (self.root/'updates'/'UPDATEFAILED').read_text())


if __name__ == '__main__':
    unittest.main()
