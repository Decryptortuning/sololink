"""Hardware-free checks for controller boot/profile fixes."""
import configparser
import importlib.machinery
import importlib.util
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('controller_clock', str(ROOT/'init/clock_sync'))
spec = importlib.util.spec_from_loader(loader.name, loader)
clock = importlib.util.module_from_spec(spec)
loader.exec_module(clock)


class ControllerPlatformTests(unittest.TestCase):
    def test_timestamp_format_and_legacy_migration(self):
        self.assertEqual(clock.parse_stamp('202610281234'), '20261028123400')
        self.assertEqual(clock.parse_stamp('20240229123456'), '20240229123456')
        for bad in ['20230229123456', '20261301123456', '', 'foo', '２０２６１０２８１２３４５６']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clock.parse_stamp(bad)

    def test_atomic_checkpoint_preserves_future_floor(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'timestamp'
            p.write_text('20990101000000\n')
            inode = p.stat().st_ino
            self.assertFalse(clock.checkpoint(p, now=0))
            self.assertEqual(inode, p.stat().st_ino)
            self.assertEqual(p.read_text(), '20990101000000\n')

    def test_checkpoint_replaces_corrupt_timestamp_with_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'timestamp'
            p.write_text('broken')
            self.assertTrue(clock.checkpoint(p, now=1793210000))
            self.assertEqual(len(p.read_text().strip()), 14)
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)
            self.assertEqual([x.name for x in Path(d).iterdir()], ['timestamp'])

    def test_checknet_is_bounded_and_does_not_reboot(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'sys').mkdir(); (p/'run').mkdir(); (p/'bin').mkdir()
            for name in ['sleep', 'logger']:
                f=p/'bin'/name; f.write_text('#!/bin/sh\nexit 0\n'); f.chmod(0o755)
            env=dict(os.environ, PATH=str(p/'bin')+':'+os.environ['PATH'], SOLO_SYS_NET=str(p/'sys'), SOLO_RUN_DIR=str(p/'run'))
            script=ROOT/'config/checknet'
            subprocess.run(['sh',str(script)],env=env,check=True,timeout=2)
            marker=p/'run/sololink-network-unavailable'
            self.assertTrue(marker.exists())
            (p/'sys/wlan0').mkdir()
            subprocess.run(['sh',str(script)],env=env,check=True,timeout=2)
            self.assertFalse(marker.exists())

    def test_ap_service_waits_without_respawn_then_executes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'bin').mkdir();cfg=p/'profile';cfg.write_text('ApEnable=False\n')
            sleep=p/'bin/sleep';sleep.write_text('#!/bin/sh\nprintf "ApEnable=True\\n" > "$SOLOLINK_CONF"\n');sleep.chmod(0o755)
            result=p/'ran'
            sock=socket.socket(socket.AF_UNIX)
            self.addCleanup(sock.close)
            sock.bind(str(p/'hostapd'))
            env=dict(os.environ, PATH=str(p/'bin')+':'+os.environ['PATH'], SOLOLINK_CONF=str(cfg), HOSTAPD_CTRL=str(p/'hostapd'))
            subprocess.run(['sh',str(ROOT/'net/usr/bin/sololink-ap-service'),'touch',str(result)],env=env,check=True,timeout=2)
            self.assertTrue(result.exists())

    def test_lan_firewall_is_idempotent_and_keeps_ap_path_separate(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);state=p/'rules.json';mock=p/'iptables'
            mock.write_text('''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
p=Path(os.environ['MOCK_RULES']);s=json.loads(p.read_text()) if p.exists() else {}
key=Path(sys.argv[0]).name;t=s.setdefault(key,{'INPUT':[]});a=sys.argv[1:];op,c=a[:2];rule=a[2:];rc=0
if op=='-N':
 if c in t:rc=1
 else:t[c]=[]
elif op=='-F':
 if c not in t:rc=1
 else:t[c]=[]
elif op=='-X':
 if c not in t:rc=1
 else:del t[c]
elif op=='-A':t[c].append(rule)
elif op=='-I':t[c].insert(0,rule[1:])
elif op=='-C':rc=0 if rule in t.get(c,[]) else 1
elif op=='-D':
 if rule in t.get(c,[]):t[c].remove(rule)
 else:rc=1
else:raise SystemExit('Unsupported mock operation '+op)
p.write_text(json.dumps(s));sys.exit(rc)
''');mock.chmod(0o755);(p/'ip6tables').symlink_to(mock)
            env=dict(os.environ, PATH=str(p)+':'+os.environ['PATH'], MOCK_RULES=str(state))
            script=ROOT/'net/usr/bin/sololink-lan-firewall'
            for _ in range(2):subprocess.run(['sh',str(script),'start'],env=env,check=True,timeout=5)
            rules=json.loads(state.read_text())
            for family in ['iptables','ip6tables']:
                self.assertEqual(rules[family]['INPUT'],[['-i','wlan0','-j','OPENSOLO_CTRL_LAN']])
                chain=rules[family]['OPENSOLO_CTRL_LAN']
                self.assertEqual(chain[-1],['-j','DROP'])
                self.assertIn(['-p','tcp','--dport','22','-j','ACCEPT'],chain)
                self.assertNotIn('wlan0-ap',str(chain))
            subprocess.run(['sh',str(script),'stop'],env=env,check=True,timeout=5)
            self.assertEqual(json.loads(state.read_text()),{'iptables':{'INPUT':[]},'ip6tables':{'INPUT':[]}})


if __name__ == '__main__':
    unittest.main()
