"""Execute only after combined source freeze. All roots are disposable folders."""
import importlib.util
import hashlib
import sys
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wifi', HERE / 'opensolo_wifi.py')
wifi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi)


class WifiLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller, self.drone = self.root / 'controller', self.root / 'drone'
        for root in (self.controller, self.drone):
            (root / 'etc').mkdir(parents=True)
            (root / 'etc/hostapd.orig').write_text('interface=wlan0-ap\nssid=SoloLink_Default\nwpa=2\nwpa_pairwise=TKIP CCMP\nwps_state=0\ncountry_code=US\nchannel=0\n')
            (root / 'etc/wpa_supplicant.orig').write_text('ctrl_interface=/var/run/wpa_supplicant\nupdate_config=0\n')
        self.ssid = 'SoloLink_Test'
        self.password = 'test-only-}\\"&/value'

    def pair(self):
        wifi.pair(self.controller, self.drone, self.root / 'operator.json', self.ssid, self.password)

    def boot(self, root):
        return subprocess.run(['bash', str(HERE / 'configinit')], env=dict(os.environ,
            CONFIGINIT_ROOT=str(root), OPENSOLO_WIFI_HELPER=str(HERE / 'opensolo_wifi.py')),
            capture_output=True, text=True, check=True)

    def setter(self, root, role, *args):
        return subprocess.run(['bash', str(HERE / 'sololink_config'), *args], env=dict(os.environ,
            ROOT=str(root), TESTHOSTNAME=role, SOLOLINK_CONFIG_TEST='1',
            OPENSOLO_WIFI_HELPER=str(HERE / 'opensolo_wifi.py')), capture_output=True, text=True)

    def test_pair_real_configinit_twice_and_private_recovery(self):
        self.pair()
        for root, kind in ((self.controller, 'hostapd'), (self.drone, 'wpa_supplicant')):
            self.boot(root)
            self.boot(root)
            text = wifi.ready(root, kind)
            self.assertEqual(wifi.value(text, kind, 'password'), self.password)
            self.assertEqual(wifi.value(text, kind, 'ssid'), self.ssid)
            self.assertEqual((root / ('etc/' + kind + '.conf')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.root / 'operator.json').read_text())['password'], self.password)
        self.assertEqual((self.root / 'operator.json').stat().st_mode & 0o777, 0o600)

    def test_wire_psk_matches_known_wpa_vector_and_punctuation_roundtrip(self):
        # WPA PBKDF2 reference vector, independent of the lifecycle's getter.
        text = wifi.set_value('network={\n}\n', 'wpa_supplicant', 'ssid', 'IEEE')
        text = wifi.set_value(text, 'wpa_supplicant', 'password', 'password')
        self.assertIn('psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e', text)
        self.pair()
        station = (self.drone / 'etc/wpa_supplicant.conf').read_text()
        psk = next(line.split('=', 1)[1] for line in station.splitlines() if line.startswith('psk='))
        self.assertEqual(psk, hashlib.pbkdf2_hmac('sha1', self.password.encode(), self.ssid.encode(), 4096, 32).hex())
        self.assertNotIn('psk="', station)
        self.assertNotIn(self.password, station)

    def test_actual_hostapd_config_setter_preserves_marker_and_boot_settings(self):
        self.pair()
        spec = importlib.util.spec_from_file_location('configfile', HERE.parent / 'net/usr/bin/configfile.py')
        configfile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(configfile)
        with mock.patch.dict(sys.modules, {'opensolo_wifi': wifi}):
            configfile.paramSet(str(self.controller / 'etc/hostapd.conf'), 'country_code', 'AU')
            configfile.paramSet(str(self.controller / 'etc/hostapd.conf'), 'channel', '6')
        self.assertTrue(wifi.valid(self.controller, 'hostapd.conf'))
        self.boot(self.controller)
        text = wifi.ready(self.controller, 'hostapd')
        self.assertIn('country_code=AU', text)
        self.assertIn('channel=6', text)
        self.assertEqual(wifi.value(text, 'hostapd', 'ssid'), self.ssid)

    def test_real_setter_missing_line_roundtrip_and_reset(self):
        self.boot(self.controller)
        self.boot(self.drone)
        for root, role, kind in ((self.controller, '3dr_controller', 'hostapd'), (self.drone, '3dr_solo', 'wpa_supplicant')):
            for option, value in (('--set-wifi-ssid', self.ssid), ('--set-wifi-password', self.password)):
                result = self.setter(root, role, option, value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(self.password, result.stdout + result.stderr)
            result = self.setter(root, role, '--get-wifi-password')
            self.assertEqual(result.stdout.strip(), self.password)
            self.boot(root)
            self.assertEqual(wifi.value(wifi.ready(root, kind), kind, 'password'), self.password)
            result = self.setter(root, role, '--reset-wifi-settings')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.boot(root)
            with self.assertRaises(ValueError):
                wifi.ready(root, kind)
            self.assertFalse((root / ('etc/' + kind + '.back')).exists())

    def test_recovery_file_cannot_alias_either_device_root(self):
        for recovery in (self.controller / 'etc/hostapd.conf', self.drone / 'operator.json'):
            before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with self.assertRaisesRegex(ValueError, 'outside both'):
                wifi.pair(self.controller, self.drone, recovery, self.ssid, self.password)
            after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_unprovisioned_invalid_input_symlinks_do_not_enable_ap(self):
        self.boot(self.controller)
        with self.assertRaises(ValueError):
            wifi.ready(self.controller, 'hostapd')
        for value in ('short', 'long\npassword', 'x' * 64):
            with self.assertRaises(ValueError):
                wifi.pair(self.controller, self.drone, self.root / 'recovery.json', self.ssid, value)
        victim = self.root / 'victim'
        victim.write_text('preserve')
        (self.drone / 'etc/wpa_supplicant.conf').symlink_to(victim)
        with self.assertRaises(ValueError):
            self.pair()
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertFalse((self.root / 'operator.json').exists())

    def test_old_unknown_or_factory_config_fails_closed(self):
        old = 'ssid=SoloLink_Old\nwpa=2\nwps_state=2\nwpa_passphrase=old-test-only\n'
        for has_base in (False, True):
            wifi.write_config(self.controller, 'hostapd.conf', old)
            if has_base:
                wifi.write_config(self.controller, 'hostapd.base', old)
            wifi.initialize(self.controller, 'hostapd')
            with self.assertRaises(ValueError):
                wifi.ready(self.controller, 'hostapd')

    def test_unproven_backup_cannot_resurrect_after_base_migration(self):
        old = 'ssid=SoloLink_Old\nwpa=2\nwps_state=0\nwpa_passphrase=unknown-test-only\n'
        wifi.write_config(self.controller, 'hostapd.conf', old)
        wifi.write_config(self.controller, 'hostapd.back', old)
        self.boot(self.controller)
        (self.controller / 'etc/hostapd.conf').write_text('interrupted')
        self.boot(self.controller)
        with self.assertRaises(ValueError):
            wifi.ready(self.controller, 'hostapd')
        self.assertFalse((self.controller / 'etc/hostapd.back').exists())

    def test_rotated_old_config_and_template_upgrade_preserve_pair(self):
        old = 'ssid=SoloLink_Old\nwpa=2\nwps_state=2\nwpa_passphrase=old-test-only\n'
        wifi.write_config(self.controller, 'hostapd.base', old)
        wifi.write_config(self.controller, 'hostapd.conf', wifi.set_value(old, 'hostapd', 'password', self.password))
        self.boot(self.controller)
        self.assertEqual(wifi.value(wifi.ready(self.controller, 'hostapd'), 'hostapd', 'password'), self.password)
        with (self.controller / 'etc/hostapd.orig').open('a') as stream:
            stream.write('ieee80211n=1\n')
        self.boot(self.controller)
        text = wifi.ready(self.controller, 'hostapd')
        self.assertIn('ieee80211n=1', text)
        self.assertEqual(wifi.value(text, 'hostapd', 'password'), self.password)
        # No valid base means merge cannot establish credential provenance.
        (self.controller / 'etc/hostapd.base.md5').write_text('invalid')
        self.boot(self.controller)
        with self.assertRaises(ValueError):
            wifi.ready(self.controller, 'hostapd')

    def test_interrupted_write_recovers_valid_backup_without_secret_output(self):
        self.pair()
        wifi.save(self.controller, 'hostapd', wifi.set_value(wifi.ready(self.controller, 'hostapd'), 'hostapd', 'password', 'new-test-only'))
        (self.controller / 'etc/hostapd.conf').write_text('interrupted')
        result = self.boot(self.controller)
        self.assertNotIn(self.password, result.stdout + result.stderr)
        self.assertEqual(wifi.value(wifi.ready(self.controller, 'hostapd'), 'hostapd', 'password'), self.password)

    def test_reset_scrubs_secret_even_from_old_golden_template(self):
        with (self.controller / 'etc/hostapd.orig').open('a') as stream:
            stream.write('wpa_passphrase=old-test-only\n')
        self.boot(self.controller)
        self.assertNotIn('old-test-only', (self.controller / 'etc/hostapd.conf').read_text())


if __name__ == '__main__':
    unittest.main()
